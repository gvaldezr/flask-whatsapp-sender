import os
import csv
import re
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, session, Response
from flask_sqlalchemy import SQLAlchemy
from twilio.rest import Client
from dotenv import load_dotenv
import json
import requests
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from celery import Celery
import pytz 
import io 

# --- Cargar variables de entorno desde el archivo .env ---
load_dotenv()

# --- NUEVA VARIABLE PARA EL TÍTULO ---
APP_TITLE = os.getenv('APP_TITLE', 'WhatsApp Campaigns')

# --- Configuración de Flask ---
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///instance/whatsapp_campaigns.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'a-default-secret-key')
app.config['UPLOAD_FOLDER'] = 'uploads'

# --- Configuración de Celery ---
app.config['CELERY_BROKER_URL'] = os.getenv('CELERY_BROKER_URL', 'redis://localhost:6379/0')
app.config['CELERY_RESULT_BACKEND'] = os.getenv('CELERY_RESULT_BACKEND', 'redis://localhost:6379/0')

db = SQLAlchemy(app)

# --- Inicialización de Celery ---
def make_celery(app):
    celery = Celery(
        app.import_name,
        backend=app.config['CELERY_RESULT_BACKEND'],
        broker=app.config['CELERY_BROKER_URL']
    )
    celery.conf.update(app.config)

    class ContextTask(celery.Task):
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)

    celery.Task = ContextTask
    return celery

celery = make_celery(app)

# --- Configuración del cliente de Twilio ---
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
TWILIO_MESSAGING_SERVICE_SID = os.getenv('TWILIO_MESSAGING_SERVICE_SID')
WPNUMBER = os.getenv('WPNUMBER')
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN else None

# --- Decorador para proteger rutas de administrador ---
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session or session['user'].get('role') != 'admin':
            return jsonify({'message': 'Acceso denegado: se requiere rol de administrador'}), 403
        return f(*args, **kwargs)
    return decorated_function

# --- Modelos de la Base de Datos ---
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(128))
    role = db.Column(db.String(80), nullable=False, default='standard')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)
    def to_dict(self):
        return {'uid': self.id, 'email': self.email, 'role': self.role}

class Campaign(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    template_name = db.Column(db.String(200), nullable=False)
    template_body = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(50), nullable=False, default='En Cola')
    recipient_count = db.Column(db.Integer, nullable=False)
    success_count = db.Column(db.Integer, default=0)
    error_count = db.Column(db.Integer, default=0)
    scheduled_at = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    errors = db.relationship('CampaignError', backref='campaign', lazy=True, cascade="all, delete-orphan")

    def to_dict(self):
        return {
            'id': self.id, 'templateName': self.template_name, 'templateBody': self.template_body,
            'status': self.status, 'recipients': self.recipient_count,
            'success_count': self.success_count, 'error_count': self.error_count,
            'scheduledAt': self.scheduled_at.isoformat(), 'createdAt': self.created_at.isoformat()
        }

class CampaignError(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id'), nullable=False)
    recipient_phone = db.Column(db.String(50), nullable=False)
    error_message = db.Column(db.String(255), nullable=False)

    def to_dict(self):
        return {'phone': self.recipient_phone, 'error': self.error_message}

# --- Tarea de Celery ---
@celery.task
def process_campaign_task(campaign_id, temp_csv_path, template_sid, schedule_type, scheduled_at_str):
    campaign = Campaign.query.get(campaign_id)
    if not campaign:
        print(f"Error: No se encontró la campaña con ID {campaign_id}")
        return

    campaign.status = 'Procesando'
    db.session.commit()

    recipients = []
    with open(temp_csv_path, 'r', encoding='utf-8') as f:
        csv_reader = csv.reader(f)
        for row in csv_reader:
            if len(row) >= 2 and row[0] and row[1]:
                recipients.append({'phone': row[0], 'name': row[1]})
    
    os.remove(temp_csv_path)

    success_count = 0
    error_count = 0
    
    send_options = {
        'content_sid': template_sid,
        'messaging_service_sid': TWILIO_MESSAGING_SERVICE_SID,
        'from_': WPNUMBER,
    }

    if schedule_type == 'later' and scheduled_at_str:
        send_at_utc = datetime.fromisoformat(scheduled_at_str.replace('Z', '+00:00'))
        send_options['schedule_type'] = 'fixed'
        send_options['send_at'] = send_at_utc

    for recipient in recipients:
        try:
            message = twilio_client.messages.create(
                to=recipient['phone'],
                content_variables=json.dumps({'1': recipient['name']}),
                **send_options
            )
            success_count += 1
        except Exception as e:
            error_count += 1
            clean_error_msg = clean_twilio_error(str(e))
            error_entry = CampaignError(campaign_id=campaign.id, recipient_phone=recipient['phone'], error_message=clean_error_msg)
            db.session.add(error_entry)
    
    campaign.success_count = success_count
    campaign.error_count = error_count
    
    if schedule_type == 'now':
        campaign.status = 'Enviada' if error_count == 0 else 'Enviada con Errores'
    else:
        if success_count > 0 and error_count > 0:
            campaign.status = 'Programada con Errores'
        elif success_count > 0 and error_count == 0:
            campaign.status = 'Programada'
        else:
            campaign.status = 'Error en Programación'

    db.session.commit()

# --- Funciones Auxiliares ---
def clean_twilio_error(error_string):
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    clean_string = ansi_escape.sub('', error_string)
    marker = "Twilio returned the following information:"
    if marker in clean_string:
        main_error = clean_string.split(marker)[1]
        final_message = main_error.split("More information may be available here:")[0]
        return final_message.strip()
    return clean_string

def get_message_data(limit):
    """Función auxiliar para obtener y procesar datos de mensajes de Twilio."""
    from_number = WPNUMBER
    if not from_number.startswith('whatsapp:'):
        from_number = f"whatsapp:{from_number}"
    
    messages = twilio_client.messages.list(
        from_=from_number,
        limit=int(limit)
    )
    
    status_counts = {
        'delivered': 0, 'read': 0, 'sent': 0, 'queued': 0, 'sending': 0,
        'failed': 0, 'undelivered': 0, 'canceled': 0
    }
    
    message_details = []
    local_tz = pytz.timezone('America/Mexico_City') # Zona horaria local

    for record in messages:
        if record.status in status_counts:
            status_counts[record.status] += 1
        
        # Convertir fecha a zona horaria local
        date_sent_local = record.date_sent.replace(tzinfo=pytz.utc).astimezone(local_tz) if record.date_sent else None

        message_details.append({
            "to": record.to,
            "date_sent": date_sent_local.strftime('%Y-%m-%d %H:%M:%S') if date_sent_local else 'N/A',
            "body": record.body,
            "status": record.status
        })
            
    return {"stats": status_counts, "messages": message_details}

# --- Rutas de la Aplicación ---
@app.route('/')
def index():
    return render_template('index.html', app_title=APP_TITLE)

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    email, password = data.get('email'), data.get('password')
    user = User.query.filter_by(email=email).first()
    if user and user.check_password(password):
        session['user'] = user.to_dict()
        return jsonify({'success': True, 'user': user.to_dict()})
    return jsonify({'success': False, 'message': 'Email o contraseña incorrectos'}), 401

@app.route('/api/logout', methods=['POST'])
def logout():
    session.pop('user', None)
    return jsonify({'success': True})

@app.route('/api/session', methods=['GET'])
def get_session():
    return jsonify({'user': session.get('user')})

@app.route('/api/users', methods=['GET'])
@admin_required
def get_users():
    users = User.query.all()
    return jsonify([user.to_dict() for user in users])

@app.route('/api/users', methods=['POST'])
@admin_required
def create_user():
    data = request.json
    if User.query.filter_by(email=data['email']).first():
        return jsonify({'message': 'El email ya está en uso'}), 400
    new_user = User(email=data['email'], role=data.get('role', 'standard'))
    new_user.set_password(data['password'])
    db.session.add(new_user)
    db.session.commit()
    return jsonify(new_user.to_dict()), 201

@app.route('/api/users/<int:user_id>', methods=['PUT'])
@admin_required
def update_user(user_id):
    user = User.query.get_or_404(user_id)
    data = request.json
    user.email = data.get('email', user.email)
    user.role = data.get('role', user.role)
    if 'password' in data and data['password']:
        user.set_password(data['password'])
    db.session.commit()
    return jsonify(user.to_dict())

@app.route('/api/users/<int:user_id>', methods=['DELETE'])
@admin_required
def delete_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == session['user']['uid']:
        return jsonify({'message': 'No puedes eliminar tu propia cuenta'}), 400
    db.session.delete(user)
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/templates', methods=['GET'])
def get_templates():
    if 'user' not in session: return jsonify({'message': 'No autorizado'}), 403
    if not twilio_client: return jsonify([{'sid': 'HX123', 'friendly_name': 'egresados_bienvenida', 'body': 'Cuerpo de ejemplo.'}])
    try:
        all_contents = twilio_client.content.v1.contents.list()
        filtered_templates = []
        for t in all_contents:
            if t.friendly_name and t.friendly_name.lower().startswith('egresados'):
                body = ""
                if t.types:
                    for template_type in ['twilio/text', 'twilio/media', 'twilio/whatsapp-template', 'whatsapp/card', 'twilio/call-to-action', 'twilio/flows']:
                        if template_type in t.types:
                            body = t.types[template_type].get('body', '')
                            break
                filtered_templates.append({'sid': t.sid, 'friendly_name': t.friendly_name, 'body': body})
        return jsonify(filtered_templates)
    except Exception as e:
        print(f"Error al obtener plantillas de Twilio: {e}")
        return jsonify({'message': 'No se pudieron obtener las plantillas de Twilio.'}), 500

@app.route('/api/templates', methods=['POST'])
@admin_required
def create_template():
    data = request.json
    name = data.get('name')
    template_type = data.get('type')
    body = data.get('body')
    media_url = data.get('mediaUrl')

    if not all([name, template_type, body]):
        return jsonify({'message': 'Nombre, tipo y cuerpo son requeridos.'}), 400

    types_payload = {}
    if template_type == 'twilio/text':
        types_payload = {'twilio/text': {'body': body}}
    elif template_type == 'twilio/media':
        if not media_url:
            return jsonify({'message': 'La URL del medio es requerida para plantillas de media.'}), 400
        types_payload = {'twilio/media': {'body': body, 'media': [media_url]}}
    else:
        return jsonify({'message': 'Tipo de plantilla no válido.'}), 400
    
    content_payload = {
        "friendly_name": name,
        "language": "es",
        "variables": {'1': 'Nombre del Cliente'},
        "types": types_payload
    }

    try:
        content_url = f"https://content.twilio.com/v1/Content"
        auth = (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        
        response = requests.post(content_url, auth=auth, json=content_payload)
        response.raise_for_status()
        content_data = response.json()
        content_sid = content_data['sid']

        approval_url = f"https://content.twilio.com/v1/Content/{content_sid}/ApprovalRequests/whatsapp"
        approval_payload = {'name': name, 'category': 'MARKETING'}
        
        approval_response = requests.post(approval_url, auth=auth, json=approval_payload)
        approval_response.raise_for_status()
        approval_data = approval_response.json()

        return jsonify({'success': True, 'sid': content_sid, 'approval_status': approval_data.get('status', 'submitted')}), 201
    except requests.exceptions.HTTPError as e:
        error_details = e.response.json()
        clean_error = error_details.get('message', str(e))
        print(f"Error al crear plantilla en Twilio: {clean_error}")
        return jsonify({'message': f"Error de Twilio: {clean_error}"}), e.response.status_code
    except Exception as e:
        print(f"Error inesperado al crear plantilla: {e}")
        return jsonify({'message': 'Ocurrió un error inesperado.'}), 500

@app.route('/api/campaigns', methods=['GET'])
def get_campaigns():
    if 'user' not in session: return jsonify({'message': 'No autorizado'}), 403
    user_campaigns = Campaign.query.filter_by(user_id=session['user']['uid']).order_by(Campaign.created_at.desc()).all()
    return jsonify([c.to_dict() for c in user_campaigns])


@app.route('/api/campaigns/<int:campaign_id>/errors', methods=['GET'])
def get_campaign_errors(campaign_id):
    if 'user' not in session: return jsonify({'message': 'No autorizado'}), 403
    campaign = Campaign.query.filter_by(id=campaign_id, user_id=session['user']['uid']).first_or_404()
    return jsonify([error.to_dict() for error in campaign.errors])

@app.route('/api/campaigns', methods=['POST'])
def create_campaign():
    if 'user' not in session: return jsonify({'message': 'No autorizado'}), 403

    template_sid = request.form.get('templateId')
    template_name = request.form.get('templateName')
    schedule_type = request.form.get('scheduleType')
    scheduled_at_str = request.form.get('scheduledAt')
    csv_file = request.files.get('csvFile')

    template_body = ""
    try:
        content = twilio_client.content.v1.contents(template_sid).fetch()
        if content.types:
            for template_type in ['twilio/text', 'twilio/media', 'twilio/whatsapp-template', 'whatsapp/card', 'twilio/call-to-action', 'twilio/flows']:
                if template_type in content.types:
                    template_body = content.types[template_type].get('body', '')
                    break
    except Exception as e:
        print(f"No se pudo obtener el cuerpo de la plantilla {template_sid}: {e}")

    if not os.path.exists(app.config['UPLOAD_FOLDER']):
        os.makedirs(app.config['UPLOAD_FOLDER'])
    
    csv_content = csv_file.stream.read().decode("utf-8")
    csv_file.stream.seek(0) 
    recipients_count = len(csv_content.splitlines())

    scheduled_at_utc = datetime.fromisoformat(scheduled_at_str.replace('Z', '+00:00')) if schedule_type == 'later' and scheduled_at_str else datetime.utcnow()
    
    new_campaign = Campaign(
        template_name=template_name,
        template_body=template_body,
        status='En Cola',
        recipient_count=recipients_count,
        scheduled_at=scheduled_at_utc,
        user_id=session['user']['uid']
    )
    db.session.add(new_campaign)
    db.session.commit()

    temp_csv_path = os.path.join(app.config['UPLOAD_FOLDER'], f"campaign_{new_campaign.id}.csv")
    with open(temp_csv_path, 'w', encoding='utf-8') as f:
        f.write(csv_content)

    process_campaign_task.delay(new_campaign.id, temp_csv_path, template_sid, schedule_type, scheduled_at_str)

    return jsonify(new_campaign.to_dict()), 202


@app.route('/api/campaigns/<int:campaign_id>', methods=['DELETE'])
def delete_campaign(campaign_id):
    if 'user' not in session: return jsonify({'message': 'No autorizado'}), 403
    campaign = Campaign.query.filter_by(id=campaign_id, user_id=session['user']['uid']).first_or_404()
    db.session.delete(campaign)
    db.session.commit()
    return jsonify({'success': True})


# --- RUTA DE REPORTES MODIFICADA ---
@app.route('/api/reports', methods=['GET'])
def get_reports():
    if 'user' not in session:
        return jsonify({'message': 'No autorizado'}), 403
    if not twilio_client or not WPNUMBER:
        return jsonify({'message': 'Twilio no está configurado en el servidor.'}), 500
    
    limit = request.args.get('limit', '1000') # Default to 1000 messages
        
    try:
        data = get_message_data(limit)
        return jsonify(data)
    except Exception as e:
        print(f"Error al obtener reportes de Twilio: {e}")
        return jsonify({'message': 'No se pudieron obtener los reportes de Twilio.'}), 500

# --- NUEVA RUTA PARA DESCARGAR CSV ---
@app.route('/api/reports/download', methods=['GET'])
def download_report():
    if 'user' not in session:
        return jsonify({'message': 'No autorizado'}), 403
    
    limit = request.args.get('limit', '1000')

    try:
        data = get_message_data(limit)
        
        output = io.StringIO()
        writer = csv.writer(output)
        
        # Escribir cabeceras
        writer.writerow(['Destinatario', 'Fecha de Envío (Local)', 'Cuerpo del Mensaje', 'Estatus'])
        
        # Escribir datos
        for message in data['messages']:
            writer.writerow([message['to'], message['date_sent'], message['body'], message['status']])
        
        output.seek(0)
        
        return Response(
            output,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment;filename=reporte_mensajes_{datetime.now().strftime('%Y%m%d')}.csv"}
        )

    except Exception as e:
        print(f"Error al generar CSV: {e}")
        return jsonify({'message': 'No se pudo generar el reporte en CSV.'}), 500

# --- Comandos de CLI ---
@app.cli.command("init-db")
def init_db_command():
    if not os.path.exists('instance'):
        os.makedirs('instance')
    db.create_all()
    if not User.query.filter_by(email='admin@example.com').first():
        admin = User(email='admin@example.com', role='admin')
        admin.set_password('password')
        db.session.add(admin)
    if not User.query.filter_by(email='user@example.com').first():
        user = User(email='user@example.com', role='standard')
        user.set_password('password')
        db.session.add(user)
    db.session.commit()
    print("Base de datos inicializada y usuarios creados con contraseña 'password'.")


if __name__ == '__main__':
    app.run(debug=True, port=5001)


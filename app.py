import os
import csv
import re
from datetime import datetime
from flask import Flask, render_template, request, jsonify, session
from flask_sqlalchemy import SQLAlchemy
from twilio.rest import Client
from dotenv import load_dotenv
import json
import requests
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from celery import Celery, Task
from werkzeug.middleware.proxy_fix import ProxyFix
# --- Cargar variables de entorno desde el archivo .env ---
load_dotenv()

# --- Configuración de Flask ---
app = Flask(__name__)
# --- Cambio Requerido: Añadir el middleware ProxyFix ---
# Esta línea le permite a Flask entender que está detrás de un proxy (Apache)
# y manejar correctamente las URLs bajo el prefijo /wp
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
# Ubicación de la base de datos SQLite
# NOTA: La ruta se mantiene simple, Docker se encargará de la persistencia con volúmenes.
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///whatsapp_campaigns.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'a-default-secret-key')
app.config['UPLOAD_FOLDER'] = 'uploads'

# --- Configuración de Celery ---
# MODIFICACIÓN CLAVE: Usar variables de entorno para que apunte al contenedor de Redis.
# 'redis' es el nombre del servicio en docker-compose.yml
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

    class ContextTask(Task):
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

# --- Tarea de Celery para procesar la campaña en segundo plano ---
@celery.task
def process_campaign_task(campaign_id, temp_csv_path, template_sid, schedule_type, scheduled_at_str):
    # Ya no se necesita el with app.app_context() gracias a la clase ContextTask
    campaign = Campaign.query.get(campaign_id)
    if not campaign:
        print(f"Error: No se encontró la campaña con ID {campaign_id}")
        return

    campaign.status = 'Procesando'
    db.session.commit()

    recipients = []
    try:
        with open(temp_csv_path, 'r', encoding='utf-8') as f:
            csv_reader = csv.reader(f)
            for row in csv_reader:
                if len(row) >= 2 and row[0] and row[1]:
                    recipients.append({'phone': row[0], 'name': row[1]})
        
        os.remove(temp_csv_path)
    except FileNotFoundError:
        print(f"Error: El archivo temporal {temp_csv_path} no fue encontrado. La tarea no puede continuar.")
        campaign.status = 'Error Interno'
        db.session.commit()
        return


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

# --- Función para limpiar mensajes de error de Twilio ---
def clean_twilio_error(error_string):
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    clean_string = ansi_escape.sub('', error_string)
    marker = "Twilio returned the following information:"
    if marker in clean_string:
        main_error = clean_string.split(marker)[1]
        final_message = main_error.split("More information may be available here:")[0]
        return final_message.strip()
    return clean_string

# --- Rutas de la Aplicación ---
@app.route('/')
def index():
    return render_template('index.html')

# ... (El resto de tus rutas @app.route se mantienen exactamente igual) ...
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
    if session.get('user') and user.id == session['user']['uid']:
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
                    if 'twilio/text' in t.types: body = t.types['twilio/text'].get('body', '')
                    elif 'twilio/call-to-action' in t.types: body = t.types['twilio/call-to-action'].get('body', '')
                    elif 'twilio/media' in t.types: body = t.types['twilio/media'].get('body', '')
                    elif 'twilio/whatsapp-template' in t.types: body = t.types['twilio/whatsapp-template'].get('body', '')
                    elif 'twilio/flows' in t.types: body = t.types['twilio/flows'].get('body', '')
                    elif 'whatsapp/card' in t.types: body = t.types['whatsapp/card'].get('body', '')
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
            if 'twilio/text' in content.types: template_body = content.types['twilio/text'].get('body', '')
            elif 'twilio/call-to-action' in content.types: template_body = content.types['twilio/call-to-action'].get('body', '')
            elif 'twilio/media' in content.types: template_body = content.types['twilio/media'].get('body', '')
            elif 'twilio/whatsapp-template' in content.types: template_body = content.types['twilio/whatsapp-template'].get('body', '')
            elif 'twilio/flows' in content.types: template_body = content.types['twilio/flows'].get('body', '')
            elif 'whatsapp/card' in content.types: template_body = content.types['whatsapp/card'].get('body', '')
    except Exception as e:
        print(f"No se pudo obtener el cuerpo de la plantilla {template_sid}: {e}")

    if not os.path.exists(app.config['UPLOAD_FOLDER']):
        os.makedirs(app.config['UPLOAD_FOLDER'])
    
    csv_content = csv_file.stream.read().decode("utf-8")
    csv_file.stream.seek(0) 
    recipients_count = len(csv_content.splitlines()) -1 # Restar el encabezado

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
    with open(temp_csv_path, 'w', newline='', encoding='utf-8') as f:
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


# --- Comandos CLI ---
@app.cli.command("init-db")
def init_db_command():
    """Inicializa la base de datos y crea usuarios por defecto."""
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

# Esta sección ya no es necesaria para producción con Gunicorn/Docker,
# pero se mantiene para facilitar la ejecución local si se desea.
if __name__ == '__main__':
    app.run(debug=True, port=5001, host='0.0.0.0')

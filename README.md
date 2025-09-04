**Flask WhatsApp Marketing SPA**

Aplicación de Página Única (SPA) desarrollada con Flask para la gestión y envío de campañas de marketing masivas a través de la API de WhatsApp de Twilio. El proyecto está diseñado para ser desplegado fácilmente como un servicio contenerizado utilizando Docker Compose.

**✨ Características Principales**
Despliegue Sencillo con Docker: Todo el entorno (Flask, Celery, Redis) está orquestado con Docker Compose para un despliegue rápido y consistente.

Envío de Campañas Masivas: Permite subir listas de contactos en formato CSV para enviar mensajes masivos basados en plantillas de Twilio.

Procesamiento en Segundo Plano: Utiliza Celery y Redis para procesar el envío de campañas de forma asíncrona, asegurando que la interfaz de usuario se mantenga rápida y receptiva.

Gestión de Plantillas: Interfaz para visualizar y crear nuevas plantillas de mensajes directamente en la API de Twilio.

Gestión de Usuarios: Sistema de autenticación con roles (administrador y estándar) para controlar el acceso a las diferentes funcionalidades.

Seguimiento de Campañas: Visualiza el estado de cada campaña (En Cola, Procesando, Enviada, etc.) y los resultados (envíos exitosos y con error).

**🚀 Despliegue**
Esta aplicación está configurada para funcionar detrás de un proxy inverso (como Apache o Nginx). La configuración base está preparada para que la aplicación sea accesible desde una subruta (por ejemplo, http://tudominio.com/wp).

**Prerrequisitos**
Un servidor con Ubuntu 22.04 LTS (o similar).

Docker y Docker Compose instalados.

Git.

Credenciales de una cuenta de Twilio (Account SID, Auth Token, Messaging Service SID).

**Pasos de Instalación**
Clonar el repositorio:

_git clone [https://github.com/gvaldezr/flask-whatsapp-sender.git](https://github.com/gvaldezr/flask-whatsapp-sender.git)
cd flask-whatsapp-sender_

Configurar las variables de entorno:
_Crea un archivo .env a partir del ejemplo y llénalo con tus credenciales.
_
_cp .env.example .env
nano .env_

Crear directorios necesarios:
Estos directorios son para la base de datos y los archivos CSV subidos.

_mkdir instance
mkdir uploads_

Construir y levantar los contenedores:
Este comando construirá las imágenes de Docker y lanzará los servicios en segundo plano.

_sudo docker-compose up --build -d_

Inicializar la base de datos:
Ejecuta este comando una única vez para crear las tablas y los usuarios iniciales.

_sudo docker-compose exec web flask init-db_

Configurar el Proxy Inverso:
Configura tu servidor web (Apache o Nginx) para redirigir el tráfico de una ruta específica (ej. /wp) al contenedor de la aplicación en http://localhost:5001.

🐞 Correcciones Implementadas en v1.0.0
Solucionado error de conexión a la base de datos que ocurría durante el primer arranque de los contenedores.

Corregido un error de JavaScript (lucide is not defined) que impedía la correcta visualización de los íconos en el frontend.

Creado con ❤️ para Anáhuac Mayab.

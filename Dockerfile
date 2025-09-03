# Usa una imagen base de Python oficial.
FROM python:3.9-slim

# Establece el directorio de trabajo dentro del contenedor.
WORKDIR /app

# Copia el archivo de requerimientos primero para aprovechar el cache de Docker.
COPY requirements.txt .

# Instala las dependencias de Python.
RUN pip install --no-cache-dir -r requirements.txt

# Copia el resto del código de la aplicación al directorio de trabajo.
COPY . .

# Expone el puerto en el que Gunicorn correrá la aplicación.
EXPOSE 5001

# El comando para iniciar la aplicación se definirá en docker-compose.yml
# ya que este mismo Dockerfile se usará tanto para el servidor web como para el worker de Celery.
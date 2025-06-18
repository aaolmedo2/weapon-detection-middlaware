from flask import Flask, request, jsonify, render_template, Response
import json
from datetime import datetime
import os
import sqlite3
import threading
import cv2
from pathlib import Path
import sys
import time
import logging
from logging.handlers import RotatingFileHandler

# 👉 Construir la ruta absoluta al subdirectorio donde está 'core/detector.py'
SERVICE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), 'external', 'weapon-detection-service', 'weapon-detection-service'))
sys.path.insert(0, SERVICE_PATH)

# 👉 Ahora puedes importar la clase desde el submódulo
from core.detector import WeaponDetector

# Configurar logging de Flask
flask_logger = logging.getLogger('werkzeug')
flask_logger.setLevel(logging.ERROR)  # Solo mostrar errores de Flask

app = Flask(__name__, 
           template_folder='../../templates',
           static_folder='../../static')

# Configuración
DATABASE = 'database.db'
CONFIG_FILE = 'config/cameras.json'
DETECTIONS_LOG = 'logs/detections.txt'

# Crear directorios necesarios
os.makedirs('detections', exist_ok=True)
os.makedirs('logs', exist_ok=True)

# Inicializar el detector
detector = WeaponDetector()

# Diccionario para mantener las instancias de las cámaras
active_cameras = {}
camera_frames = {}

class CameraManager:
    def __init__(self, camera_id, url):
        self.camera_id = camera_id
        self.url = url
        self.is_running = False
        self.capture = None
        self.thread = None
        self.last_frame = None

    def start(self):
        if not self.is_running:
            try:
                self.capture = cv2.VideoCapture(self.url)
                if not self.capture.isOpened():
                    raise Exception(f"No se pudo conectar a la cámara: {self.url}")
                self.is_running = True
                self.thread = threading.Thread(target=self._capture_loop)
                self.thread.daemon = True
                self.thread.start()
            except Exception as e:
                print(f"Error al iniciar la cámara {self.camera_id}: {str(e)}")
                self.stop()
                raise

    def stop(self):
        """Detiene la cámara de manera segura"""
        self.is_running = False
        if self.capture:
            try:
                self.capture.release()
            except Exception as e:
                print(f"Error al liberar la cámara {self.camera_id}: {str(e)}")
        self.capture = None
        
        # Solo intentar unir el thread si no es el thread actual
        if self.thread and self.thread != threading.current_thread():
            self.thread.join(timeout=1.0)
        self.thread = None
        
        if self.camera_id in camera_frames:
            del camera_frames[self.camera_id]

    def _capture_loop(self):
        """Procesa los frames de la cámara"""
        while self.is_running:
            try:
                if self.capture is None or not self.capture.isOpened():
                    print(f"Cámara {self.camera_id} desconectada")
                    break

                ret, frame = self.capture.read()
                if not ret:
                    print(f"Error al leer frame de la cámara {self.camera_id}")
                    break

                # Procesar frame con el detector
                processed_frame, detection_info = detector.process_frame(frame, self.camera_id)

                # Si hay detección, registrar en la base de datos
                if detection_info:
                    try:
                        self._register_detection(detection_info)
                    except Exception as e:
                        print(f"Error al registrar detección: {str(e)}")
                        continue

                # Actualizar el frame para streaming
                _, buffer = cv2.imencode('.jpg', processed_frame)
                self.last_frame = buffer.tobytes()
                camera_frames[self.camera_id] = self.last_frame

                # Pequeña pausa para no saturar el CPU
                time.sleep(0.03)

            except Exception as e:
                print(f"Error en _capture_loop para cámara {self.camera_id}: {str(e)}")
                break

        # Marcar que ya no está corriendo y limpiar recursos
        self.is_running = False
        if self.capture:
            self.capture.release()
        self.capture = None
        if self.camera_id in camera_frames:
            del camera_frames[self.camera_id]

    def _register_detection(self, detection_info):
        # Generar ID único con microsegundos y un número aleatorio
        timestamp = datetime.now()
        detection_id = f"det_{timestamp.strftime('%Y%m%d_%H%M%S')}_{timestamp.microsecond:06d}"
        
        conn = get_db()
        try:
            # Intentar insertar con reintentos en caso de colisión
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    conn.execute('''INSERT INTO detections 
                                (id, timestamp, weapon_type, confidence, camera_id, image_path)
                                VALUES (?, ?, ?, ?, ?, ?)''',
                                (detection_id,
                                detection_info['timestamp'],
                                detection_info['tipoArma'],
                                detection_info['porcentaje'],
                                detection_info['camara'],
                                f"detections/detection_{detection_id}.jpg"))
                    conn.commit()
                    break
                except sqlite3.IntegrityError:
                    if attempt == max_retries - 1:
                        raise
                    # Si hay colisión, generar nuevo ID
                    detection_id = f"det_{timestamp.strftime('%Y%m%d_%H%M%S')}_{timestamp.microsecond:06d}_{attempt + 1}"
        finally:
            conn.close()

    def get_frame(self):
        return self.last_frame

@app.route('/video_feed/<camera_id>')
def video_feed(camera_id):
    """Genera el stream de video para una cámara específica"""
    def generate():
        while True:
            if camera_id in active_cameras and camera_frames.get(camera_id):
                frame = camera_frames[camera_id]
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
            
    return Response(generate(),
                   mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/active_cameras')
def active_cameras_view():
    """Vista de cámaras activas"""
    conn = get_db()
    cameras = conn.execute('SELECT * FROM cameras WHERE status = "active"').fetchall()
    conn.close()
    return render_template('active_cameras.html', cameras=cameras)

def init_db():
    """Inicializa la base de datos"""
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    
    # Crear tabla de detecciones
    c.execute('''CREATE TABLE IF NOT EXISTS detections
                 (id TEXT PRIMARY KEY,
                  timestamp TEXT,
                  weapon_type TEXT,
                  confidence REAL,
                  camera_id TEXT,
                  image_path TEXT)''')
    
    # Crear tabla de cámaras
    c.execute('''CREATE TABLE IF NOT EXISTS cameras
                 (id TEXT PRIMARY KEY,
                  name TEXT,
                  url TEXT,
                  status TEXT)''')
    
    conn.commit()
    conn.close()

def get_db():
    """Obtiene conexión a la base de datos"""
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

@app.route('/')
def home():
    """Página principal del sistema"""
    return render_template('index.html')

@app.route('/cameras')
def list_cameras():
    """Lista todas las cámaras registradas"""
    conn = get_db()
    cameras = conn.execute('SELECT * FROM cameras').fetchall()
    conn.close()
    return render_template('cameras.html', cameras=cameras)

@app.route('/api/cameras', methods=['GET'])
def get_cameras():
    """API: Obtiene lista de cámaras"""
    conn = get_db()
    cameras = conn.execute('SELECT * FROM cameras').fetchall()
    conn.close()
    return jsonify([dict(camera) for camera in cameras])

@app.route('/api/cameras', methods=['POST'])
def add_camera():
    """API: Agrega una nueva cámara"""
    data = request.json
    if not all(k in data for k in ['id', 'name', 'url']):
        return jsonify({'error': 'Faltan campos requeridos'}), 400
        
    # Verificar si la cámara es accesible
    try:
        cap = cv2.VideoCapture(data['url'])
        if not cap.isOpened():
            return jsonify({'error': 'No se puede acceder a la cámara'}), 400
        cap.release()
    except Exception as e:
        return jsonify({'error': f'Error al verificar la cámara: {str(e)}'}), 400

    conn = get_db()
    try:
        conn.execute('INSERT INTO cameras (id, name, url, status) VALUES (?, ?, ?, ?)',
                    (data['id'], data['name'], data['url'], 'inactive'))
        conn.commit()
        return jsonify({'message': 'Cámara agregada exitosamente'}), 201
    except sqlite3.IntegrityError:
        return jsonify({'error': 'La cámara ya existe'}), 409
    finally:
        conn.close()

@app.route('/api/cameras/<camera_id>', methods=['PUT'])
def update_camera(camera_id):
    """API: Actualiza una cámara existente"""
    data = request.json
    conn = get_db()
    
    try:
        if 'status' in data:
            if data['status'] == 'active':
                # Iniciar la cámara
                if camera_id not in active_cameras:
                    camera_data = conn.execute('SELECT * FROM cameras WHERE id = ?', 
                                            (camera_id,)).fetchone()
                    if camera_data:
                        camera = CameraManager(camera_id, camera_data['url'])
                        try:
                            camera.start()
                            active_cameras[camera_id] = camera
                        except Exception as e:
                            return jsonify({'error': f'Error al iniciar la cámara: {str(e)}'}), 400
            else:
                # Detener la cámara
                if camera_id in active_cameras:
                    active_cameras[camera_id].stop()
                    del active_cameras[camera_id]

        conn.execute('UPDATE cameras SET status = ? WHERE id = ?',
                    (data['status'], camera_id))
        conn.commit()
        return jsonify({'message': 'Cámara actualizada exitosamente'})
    finally:
        conn.close()

@app.route('/api/cameras/<camera_id>', methods=['DELETE'])
def delete_camera(camera_id):
    """API: Elimina una cámara"""
    # Detener la cámara si está activa
    if camera_id in active_cameras:
        active_cameras[camera_id].stop()
        del active_cameras[camera_id]

    conn = get_db()
    try:
        conn.execute('DELETE FROM cameras WHERE id = ?', (camera_id,))
        conn.commit()
        return jsonify({'message': 'Cámara eliminada exitosamente'})
    finally:
        conn.close()

@app.route('/api/detection', methods=['POST'])
def register_detection():
    """API: Registra una nueva detección"""
    try:
        data = request.json
        required_fields = ['id', 'timestamp', 'tipoArma', 'porcentaje', 'camara', 'image_path']
        
        if not all(field in data for field in required_fields):
            return jsonify({'error': 'Faltan campos requeridos'}), 400
            
        conn = get_db()
        try:
            conn.execute('''INSERT INTO detections 
                           (id, timestamp, weapon_type, confidence, camera_id, image_path)
                           VALUES (?, ?, ?, ?, ?, ?)''',
                        (data['id'],
                         data['timestamp'],
                         data['tipoArma'],
                         data['porcentaje'],
                         data['camara'],
                         data['image_path']))
            conn.commit()
            return jsonify({'message': 'Detección registrada exitosamente'}), 201
        finally:
            conn.close()
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/detections')
def view_detections():
    """Vista de detecciones"""
    conn = get_db()
    detections = conn.execute('''SELECT * FROM detections 
                                ORDER BY timestamp DESC''').fetchall()
    conn.close()
    return render_template('detections.html', detections=detections)

@app.route('/api/detections', methods=['GET'])
def get_detections():
    """API: Obtiene lista de detecciones"""
    conn = get_db()
    detections = conn.execute('''SELECT * FROM detections 
                                ORDER BY timestamp DESC''').fetchall()
    conn.close()
    return jsonify([dict(detection) for detection in detections])

def start_middleware():
    """Inicia el servidor middleware"""
    # Crear directorios necesarios
    os.makedirs('logs', exist_ok=True)
    os.makedirs('config', exist_ok=True)
    
    # Inicializar base de datos
    init_db()
    
    # Iniciar servidor Flask
    app.run(host='0.0.0.0', port=5000, debug=False)

if __name__ == '__main__':
    start_middleware() 
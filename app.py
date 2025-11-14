from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO
import threading
import time
import re
import sqlite3
from datetime import datetime
import uuid
import json
import socketio as client_socketio

app = Flask(__name__)
app.config['SECRET_KEY'] = 'IloveHazelandAngelPlushie'
socketio_app = SocketIO(app, cors_allowed_origins="*")

# === НАСТРОЙКИ DONATIONALERTS ===
DA_TOKEN = "yD7udoHZME6u5RAb9QvN"  # замените на свой токен
DA_SOCKET = None


# === БАЗА ДАННЫХ ===
class Database:
    def __init__(self):
        self.init_db()

    def init_db(self):
        conn = sqlite3.connect('pixels.db')
        c = conn.cursor()

        # Таблица пикселей
        c.execute('''CREATE TABLE IF NOT EXISTS pixels
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     x INTEGER NOT NULL,
                     y INTEGER NOT NULL,
                     emoji TEXT NOT NULL,
                     username TEXT NOT NULL,
                     order_id TEXT,
                     purchased_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

        # Таблица заказов
        c.execute('''CREATE TABLE IF NOT EXISTS orders
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     order_id TEXT UNIQUE NOT NULL,
                     cells_data TEXT NOT NULL,
                     amount REAL NOT NULL,
                     status TEXT DEFAULT 'pending',
                     created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

        # Таблица для кэша донатов
        c.execute('''CREATE TABLE IF NOT EXISTS donations_cache
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     username TEXT NOT NULL,
                     message TEXT,
                     amount REAL,
                     order_id TEXT,
                     processed BOOLEAN DEFAULT FALSE,
                     created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

        conn.commit()
        conn.close()

    def get_pixel(self, x, y):
        conn = sqlite3.connect('pixels.db')
        c = conn.cursor()
        c.execute('SELECT * FROM pixels WHERE x = ? AND y = ?', (x, y))
        pixel = c.fetchone()
        conn.close()
        return pixel

    def set_pixel(self, x, y, emoji, username, order_id=None):
        conn = sqlite3.connect('pixels.db')
        c = conn.cursor()
        existing = self.get_pixel(x, y)
        if existing:
            c.execute('UPDATE pixels SET emoji = ?, username = ?, order_id = ?, purchased_at = ? WHERE x = ? AND y = ?',
                     (emoji, username, order_id, datetime.now(), x, y))
        else:
            c.execute('INSERT INTO pixels (x, y, emoji, username, order_id) VALUES (?, ?, ?, ?, ?)',
                     (x, y, emoji, username, order_id))
        conn.commit()
        conn.close()

    def get_all_pixels(self):
        conn = sqlite3.connect('pixels.db')
        c = conn.cursor()
        c.execute('SELECT x, y, emoji, username FROM pixels')
        pixels = c.fetchall()
        conn.close()
        return [{'x': p[0], 'y': p[1], 'emoji': p[2], 'username': p[3]} for p in pixels]

    def create_order(self, cells_data, amount):
        order_id = str(uuid.uuid4())[:8]
        conn = sqlite3.connect('pixels.db')
        c = conn.cursor()
        c.execute('INSERT INTO orders (order_id, cells_data, amount) VALUES (?, ?, ?)',
                 (order_id, json.dumps(cells_data), amount))
        conn.commit()
        conn.close()
        return order_id

    def get_order(self, order_id):
        conn = sqlite3.connect('pixels.db')
        c = conn.cursor()
        c.execute('SELECT * FROM orders WHERE order_id = ?', (order_id,))
        order = c.fetchone()
        conn.close()
        return order

    def update_order_status(self, order_id, status):
        conn = sqlite3.connect('pixels.db')
        c = conn.cursor()
        c.execute('UPDATE orders SET status = ? WHERE order_id = ?', (status, order_id))
        conn.commit()
        conn.close()

    def cache_donation(self, username, message, amount, order_id=None):
        conn = sqlite3.connect('pixels.db')
        c = conn.cursor()
        c.execute('INSERT INTO donations_cache (username, message, amount, order_id) VALUES (?, ?, ?, ?)',
                 (username, message, amount, order_id))
        conn.commit()
        conn.close()


db = Database()

# === DONATIONALERTS СОКЕТ ===
def connect_to_donationalerts():
    global DA_SOCKET
    da_sio = client_socketio.Client(logger=False, engineio_logger=False)

    @da_sio.on('connect')
    def on_connect():
        print("[DA] Connected to DonationAlerts")
        da_sio.emit('add-user', {"token": DA_TOKEN, "type": "alert_widget"})

    @da_sio.on('donation')
    def on_donation(data):
        print("[DA] New donation received")
        try:
            donation_data = json.loads(data)
            username = donation_data.get('username', 'Anonymous')
            message = donation_data.get('message', '') or ''
            amount = float(donation_data.get('amount', 0) or 0)

            print(f"💸 Donation from {username}: {amount} - {message}")

            # Ищем order_id
            m = re.search(r'order[_\s-]?([a-z0-9]+)', message.lower())
            order_id = m.group(1) if m else None

            if not order_id:
                print("⚠️ Order ID not found in message.")
                return

            # Сохраняем в кэш
            db.cache_donation(username, message, amount, order_id)

            # Обрабатываем заказ
            process_donation_message(username, amount, order_id)

        except Exception as e:
            print(f"Error processing donation: {e}")

    @da_sio.on('disconnect')
    def on_disconnect():
        print("[DA] Disconnected")

    try:
        print("[DA] Connecting to DonationAlerts...")
        da_sio.connect('wss://socket.donationalerts.ru:443', transports='websocket')
        DA_SOCKET = da_sio
    except Exception as e:
        print(f"[DA] Connection failed: {e}")


# === ОБРАБОТКА ДОНОВ ===
def process_donation_message(username, real_amount, order_id):
    order = db.get_order(order_id)
    if not order:
        print(f"❌ Order {order_id} not found")
        return False

    order_amount = float(order[3])
    cells_data = json.loads(order[2])

    if real_amount >= order_amount:
        # 💰 Достаточно — ставим смайлы
        for cell in cells_data:
            db.set_pixel(cell['x'], cell['y'], cell['emoji'], username, order_id)
            socketio_app.emit('pixel_update', {
                'x': cell['x'],
                'y': cell['y'],
                'emoji': cell['emoji'],
                'username': username
            })
        db.update_order_status(order_id, 'confirmed')
        print(f"✅ Order {order_id} confirmed for {username} ({real_amount}₽ >= {order_amount}₽)")
        return True
    else:
        # 💸 Недостаточно — отклоняем
        db.update_order_status(order_id, 'rejected')
        print(f"❌ Order {order_id} rejected ({real_amount}₽ < {order_amount}₽)")
        return False


# === FLASK API ===
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/pixels')
def get_pixels():
    return jsonify(db.get_all_pixels())

@app.route('/api/buy_cells', methods=['POST'])
def buy_cells():
    try:
        data = request.json
        cells = data.get('cells', [])
        if not cells:
            return jsonify({'error': 'No cells selected'}), 400

        # Проверяем, не заняты ли клетки
        for cell in cells:
            if db.get_pixel(cell['x'], cell['y']):
                return jsonify({'error': f'Cell ({cell["x"]},{cell["y"]}) already taken'}), 400

        amount = len(cells) * 1.0  # 1 рубль за клетку
        order_id = db.create_order(cells, amount)
        payment_message = f"order_{order_id}"

        return jsonify({
            'order_number': order_id,
            'amount': amount,
            'cell_count': len(cells),
            'payment_message': payment_message
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/check_payment/<order_id>')
def check_payment(order_id):
    order = db.get_order(order_id)
    if not order:
        return jsonify({'error': 'Order not found'}), 404

    status = order[4]
    amount = order[3]
    return jsonify({
        'status': status,
        'order_id': order_id,
        'amount': amount
    })


# === SOCKET.IO ===
@socketio_app.on('connect')
def handle_connect():
    print('🟢 Client connected')

@socketio_app.on('disconnect')
def handle_disconnect():
    print('🔴 Client disconnected')


# === ЗАПУСК ===
def start_da_connection():
    time.sleep(2)
    connect_to_donationalerts()

import os
if __name__ == '__main__':
    threading.Thread(target=start_da_connection, daemon=True).start()
    port = int(os.environ.get('PORT', 5000))  # Берёт PORT из окружения или 5000 по умолчанию
    socketio_app.run(app, debug=True, host='0.0.0.0', port=port, allow_unsafe_werkzeug=True)

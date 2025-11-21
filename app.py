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
socketio_app = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# === НАСТРОЙКИ DONATIONALERTS ===
DA_TOKEN = "yD7udoHZME6u5RAb9QvN"
DA_SOCKET = None

# === IN-MEMORY БАЗА ДАННЫХ ===
class Database:
    def __init__(self):
        self.conn = sqlite3.connect(':memory:', check_same_thread=False)
        self.init_db()
        # Кэш для хранения данных
        self.pixels_cache = []
        self.orders_cache = {}
        self.promocodes_cache = {'promocodena18rubley': {'uses_left': 3, 'max_uses': 3, 'discount_cells': 18}}

    def init_db(self):
        c = self.conn.cursor()

        c.execute('''CREATE TABLE IF NOT EXISTS pixels
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     x INTEGER NOT NULL,
                     y INTEGER NOT NULL,
                     emoji TEXT NOT NULL,
                     username TEXT NOT NULL,
                     order_id TEXT,
                     purchased_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

        c.execute('''CREATE TABLE IF NOT EXISTS orders
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     order_id TEXT UNIQUE NOT NULL,
                     cells_data TEXT NOT NULL,
                     amount REAL NOT NULL,
                     status TEXT DEFAULT 'pending',
                     promocode TEXT,
                     created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

        c.execute('''CREATE TABLE IF NOT EXISTS promocodes
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     code TEXT UNIQUE NOT NULL,
                     uses_left INTEGER NOT NULL,
                     max_uses INTEGER NOT NULL,
                     discount_cells INTEGER NOT NULL,
                     created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

        # Инициализируем промокод
        c.execute('SELECT * FROM promocodes WHERE code = ?', ('promocodena18rubley',))
        if not c.fetchone():
            c.execute('INSERT INTO promocodes (code, uses_left, max_uses, discount_cells) VALUES (?, ?, ?, ?)',
                     ('promocodena18rubley', 3, 3, 18))

        self.conn.commit()

    def get_pixel(self, x, y):
        c = self.conn.cursor()
        c.execute('SELECT * FROM pixels WHERE x = ? AND y = ?', (x, y))
        return c.fetchone()

    def set_pixel(self, x, y, emoji, username, order_id=None):
        c = self.conn.cursor()
        existing = self.get_pixel(x, y)
        if existing:
            c.execute('UPDATE pixels SET emoji = ?, username = ?, order_id = ?, purchased_at = ? WHERE x = ? AND y = ?',
                     (emoji, username, order_id, datetime.now(), x, y))
        else:
            c.execute('INSERT INTO pixels (x, y, emoji, username, order_id) VALUES (?, ?, ?, ?, ?)',
                     (x, y, emoji, username, order_id))
        self.conn.commit()
        
        # Отправляем событие всем клиентам
        socketio_app.emit('pixel_update', {
            'x': x,
            'y': y,
            'emoji': emoji,
            'username': username
        }, broadcast=True)

    def get_all_pixels(self):
        c = self.conn.cursor()
        c.execute('SELECT x, y, emoji, username FROM pixels')
        pixels = c.fetchall()
        return [{'x': p[0], 'y': p[1], 'emoji': p[2], 'username': p[3]} for p in pixels]

    def create_order(self, cells_data, amount, promocode=None):
        order_id = str(uuid.uuid4())[:8]
        c = self.conn.cursor()
        c.execute('INSERT INTO orders (order_id, cells_data, amount, promocode) VALUES (?, ?, ?, ?)',
                 (order_id, json.dumps(cells_data), amount, promocode))
        self.conn.commit()
        return order_id

    def get_order(self, order_id):
        c = self.conn.cursor()
        c.execute('SELECT * FROM orders WHERE order_id = ?', (order_id,))
        return c.fetchone()

    def update_order_status(self, order_id, status):
        c = self.conn.cursor()
        c.execute('UPDATE orders SET status = ? WHERE order_id = ?', (status, order_id))
        self.conn.commit()

    def get_promocode(self, code):
        c = self.conn.cursor()
        c.execute('SELECT * FROM promocodes WHERE code = ?', (code,))
        promocode = c.fetchone()
        if promocode:
            return {
                'code': promocode[1],
                'uses_left': promocode[2],
                'max_uses': promocode[3],
                'discount_cells': promocode[4]
            }
        return None

    def use_promocode(self, code):
        c = self.conn.cursor()
        c.execute('SELECT uses_left FROM promocodes WHERE code = ?', (code,))
        result = c.fetchone()
        if result and result[0] > 0:
            c.execute('UPDATE promocodes SET uses_left = uses_left - 1 WHERE code = ?', (code,))
            self.conn.commit()
            return True
        return False

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

            m = re.search(r'order[_\s-]?([a-z0-9]+)', message.lower())
            order_id = m.group(1) if m else None

            if not order_id:
                print("⚠️ Order ID not found in message.")
                return

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
    promocode = order[5]

    if promocode:
        promocode_data = db.get_promocode(promocode)
        if promocode_data:
            for cell in cells_data:
                db.set_pixel(cell['x'], cell['y'], cell['emoji'], username, order_id)
            db.update_order_status(order_id, 'confirmed')
            print(f"✅ Order {order_id} confirmed with promocode {promocode} for {username} - FREE")
            return True
    else:
        if real_amount >= order_amount:
            for cell in cells_data:
                db.set_pixel(cell['x'], cell['y'], cell['emoji'], username, order_id)
            db.update_order_status(order_id, 'confirmed')
            print(f"✅ Order {order_id} confirmed for {username} ({real_amount}₽ >= {order_amount}₽)")
            return True
        else:
            db.update_order_status(order_id, 'rejected')
            print(f"❌ Order {order_id} rejected ({real_amount}₽ < {order_amount}₽)")
            return False

# === FLASK API ===
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/pixels')
def get_pixels():
    pixels = db.get_all_pixels()
    print(f"📊 Sending {len(pixels)} pixels to client")
    return jsonify(pixels)

@app.route('/api/buy_cells', methods=['POST'])
def buy_cells():
    try:
        data = request.json
        cells = data.get('cells', [])
        promocode = data.get('promocode', '').strip()
        
        if not cells:
            return jsonify({'error': 'No cells selected'}), 400

        # Проверяем, не заняты ли клетки
        for cell in cells:
            if db.get_pixel(cell['x'], cell['y']):
                return jsonify({'error': f'Cell ({cell["x"]},{cell["y"]}) already taken'}), 400

        # Проверяем промокод
        promocode_data = None
        if promocode:
            promocode_data = db.get_promocode(promocode)
            if not promocode_data:
                return jsonify({'error': 'Invalid promocode'}), 400
            if promocode_data['uses_left'] <= 0:
                return jsonify({'error': 'Promocode has no uses left'}), 400
            if len(cells) != promocode_data['discount_cells']:
                return jsonify({'error': f'This promocode requires exactly {promocode_data["discount_cells"]} cells'}), 400

        # Рассчитываем сумму
        if promocode_data:
            amount = 0.0
        else:
            amount = len(cells) * 1.0

        order_id = db.create_order(cells, amount, promocode if promocode_data else None)
        payment_message = f"order_{order_id}"

        # Используем промокод
        if promocode_data:
            db.use_promocode(promocode)

        return jsonify({
            'order_number': order_id,
            'amount': amount,
            'cell_count': len(cells),
            'payment_message': payment_message,
            'promocode_used': bool(promocode_data),
            'promocode_discount': promocode_data['discount_cells'] if promocode_data else 0
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/check_promocode/<code>')
def check_promocode(code):
    promocode = db.get_promocode(code)
    if not promocode:
        return jsonify({'valid': False, 'error': 'Promocode not found'})
    
    if promocode['uses_left'] <= 0:
        return jsonify({'valid': False, 'error': 'No uses left'})
    
    return jsonify({
        'valid': True,
        'code': promocode['code'],
        'uses_left': promocode['uses_left'],
        'discount_cells': promocode['discount_cells']
    })

@app.route('/api/check_payment/<order_id>')
def check_payment(order_id):
    order = db.get_order(order_id)
    if not order:
        return jsonify({'error': 'Order not found'}), 404

    status = order[4]
    amount = order[3]
    promocode = order[5]
    
    return jsonify({
        'status': status,
        'order_id': order_id,
        'amount': amount,
        'promocode_used': bool(promocode)
    })

# === SOCKET.IO ===
@socketio_app.on('connect')
def handle_connect():
    print('🟢 Client connected')
    # Отправляем текущие пиксели новому клиенту
    pixels = db.get_all_pixels()
    socketio_app.emit('initial_pixels', {'pixels': pixels})

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
    port = int(os.environ.get('PORT', 5000))
    print(f"🚀 Starting server on port {port}")
    socketio_app.run(app, debug=True, host='0.0.0.0', port=port, allow_unsafe_werkzeug=True)

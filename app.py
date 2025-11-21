from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO
import threading
import time
import re
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

# === ПРОСТАЯ IN-MEMORY БАЗА ДАННЫХ ===
class SimpleDatabase:
    def __init__(self):
        self.pixels = {}  # ключ: (x, y), значение: {emoji, username, order_id}
        self.orders = {}  # ключ: order_id, значение: {cells_data, amount, status, promocode}
        self.promocodes = {
            'promocodena18rubley': {
                'uses_left': 3, 
                'max_uses': 3, 
                'discount_cells': 18
            }
        }
        print("✅ In-memory database initialized")
    
    def get_pixel(self, x, y):
        return self.pixels.get((x, y))
    
    def set_pixel(self, x, y, emoji, username, order_id=None):
        self.pixels[(x, y)] = {
            'emoji': emoji,
            'username': username,
            'order_id': order_id,
            'timestamp': datetime.now()
        }
        print(f"✅ Pixel set: ({x}, {y}) = {emoji} by {username}")
        
        # Отправляем событие всем клиентам
        socketio_app.emit('pixel_update', {
            'x': x, 
            'y': y, 
            'emoji': emoji, 
            'username': username
        }, broadcast=True)
    
    def get_all_pixels(self):
        pixels_list = []
        for (x, y), data in self.pixels.items():
            pixels_list.append({
                'x': x, 
                'y': y, 
                'emoji': data['emoji'], 
                'username': data['username']
            })
        print(f"📊 Returning {len(pixels_list)} pixels")
        return pixels_list
    
    def create_order(self, cells_data, amount, promocode=None):
        order_id = str(uuid.uuid4())[:8]
        self.orders[order_id] = {
            'cells_data': cells_data,
            'amount': amount,
            'status': 'pending',
            'promocode': promocode,
            'created_at': datetime.now()
        }
        print(f"✅ Order created: {order_id} with {len(cells_data)} cells")
        return order_id
    
    def get_order(self, order_id):
        order = self.orders.get(order_id)
        if order:
            return (
                order_id,  # order_id
                order['cells_data'],  # cells_data (будет json строка)
                order['amount'],  # amount
                order['status'],  # status
                order['promocode']  # promocode
            )
        return None
    
    def update_order_status(self, order_id, status):
        if order_id in self.orders:
            self.orders[order_id]['status'] = status
            print(f"✅ Order {order_id} status updated to: {status}")
    
    def get_promocode(self, code):
        promocode_data = self.promocodes.get(code)
        if promocode_data:
            return {
                'code': code,
                'uses_left': promocode_data['uses_left'],
                'max_uses': promocode_data['max_uses'],
                'discount_cells': promocode_data['discount_cells']
            }
        return None
    
    def use_promocode(self, code):
        if code in self.promocodes and self.promocodes[code]['uses_left'] > 0:
            self.promocodes[code]['uses_left'] -= 1
            print(f"✅ Promocode {code} used. {self.promocodes[code]['uses_left']} uses left")
            return True
        return False

# Используем простую базу
db = SimpleDatabase()

# === DONATIONALERTS СОКЕТ ===
def connect_to_donationalerts():
    global DA_SOCKET
    da_sio = client_socketio.Client(logger=False, engineio_logger=False)

    @da_sio.on('connect')
    def on_connect():
        print("[DA] ✅ Connected to DonationAlerts")
        da_sio.emit('add-user', {"token": DA_TOKEN, "type": "alert_widget"})

    @da_sio.on('donation')
    def on_donation(data):
        print("[DA] 💸 New donation received")
        try:
            donation_data = json.loads(data)
            username = donation_data.get('username', 'Anonymous')
            message = donation_data.get('message', '') or ''
            amount = float(donation_data.get('amount', 0) or 0)

            print(f"💸 Donation from {username}: {amount} RUB - '{message}'")

            # Ищем order_id в сообщении
            m = re.search(r'order[_\s-]?([a-z0-9]+)', message.lower())
            order_id = m.group(1) if m else None

            if not order_id:
                print("⚠️ Order ID not found in message")
                return

            # Обрабатываем заказ
            process_donation_message(username, amount, order_id)

        except Exception as e:
            print(f"❌ Error processing donation: {e}")

    @da_sio.on('disconnect')
    def on_disconnect():
        print("[DA] 🔴 Disconnected from DonationAlerts")

    try:
        print("[DA] 🔄 Connecting to DonationAlerts...")
        da_sio.connect('wss://socket.donationalerts.ru:443', transports='websocket')
        DA_SOCKET = da_sio
    except Exception as e:
        print(f"[DA] ❌ Connection failed: {e}")

# === ОБРАБОТКА ДОНОВ ===
def process_donation_message(username, real_amount, order_id):
    order_data = db.get_order(order_id)
    if not order_data:
        print(f"❌ Order {order_id} not found")
        return False

    order_id, cells_data_json, order_amount, status, promocode = order_data
    cells_data = json.loads(cells_data_json)  # Десериализуем cells_data

    print(f"🔍 Processing order {order_id}: {len(cells_data)} cells, amount: {order_amount}RUB, promo: {promocode}")

    # Если использован промокод, активируем сразу
    if promocode:
        promocode_data = db.get_promocode(promocode)
        if promocode_data:
            # ✅ Промокод активирован - ставим смайлы БЕСПЛАТНО
            for cell in cells_data:
                db.set_pixel(cell['x'], cell['y'], cell['emoji'], username, order_id)
            db.update_order_status(order_id, 'confirmed')
            print(f"✅ Order {order_id} confirmed with promocode {promocode} for {username} - FREE")
            return True
    else:
        # Обычная логика без промокода
        if real_amount >= order_amount:
            # 💰 Достаточно средств - ставим смайлы
            for cell in cells_data:
                db.set_pixel(cell['x'], cell['y'], cell['emoji'], username, order_id)
            db.update_order_status(order_id, 'confirmed')
            print(f"✅ Order {order_id} confirmed for {username} ({real_amount}₽ >= {order_amount}₽)")
            return True
        else:
            # 💸 Недостаточно средств
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
    print(f"📊 API: Sending {len(pixels)} pixels to client")
    return jsonify(pixels)

@app.route('/api/buy_cells', methods=['POST'])
def buy_cells():
    try:
        data = request.json
        cells = data.get('cells', [])
        promocode = data.get('promocode', '').strip()
        
        print(f"🛒 Buy cells request: {len(cells)} cells, promocode: '{promocode}'")
        
        if not cells:
            return jsonify({'error': 'No cells selected'}), 400

        # Проверяем, не заняты ли клетки
        for cell in cells:
            if db.get_pixel(cell['x'], cell['y']):
                error_msg = f'Cell ({cell["x"]},{cell["y"]}) already taken'
                print(f"❌ {error_msg}")
                return jsonify({'error': error_msg}), 400

        # Проверяем промокод
        promocode_data = None
        if promocode:
            promocode_data = db.get_promocode(promocode)
            if not promocode_data:
                print(f"❌ Invalid promocode: {promocode}")
                return jsonify({'error': 'Invalid promocode'}), 400
            if promocode_data['uses_left'] <= 0:
                print(f"❌ Promocode {promocode} has no uses left")
                return jsonify({'error': 'Promocode has no uses left'}), 400
            if len(cells) != promocode_data['discount_cells']:
                error_msg = f'This promocode requires exactly {promocode_data["discount_cells"]} cells'
                print(f"❌ {error_msg}")
                return jsonify({'error': error_msg}), 400

        # Рассчитываем сумму
        if promocode_data:
            amount = 0.0  # 🎉 БЕСПЛАТНО!
        else:
            amount = len(cells) * 1.0  # 1 рубль за клетку

        # Создаем заказ (сериализуем cells_data в JSON)
        order_id = db.create_order(
            cells_data=cells,
            amount=amount,
            promocode=promocode if promocode_data else None
        )
        payment_message = f"order_{order_id}"

        # Используем промокод если он валидный
        if promocode_data:
            db.use_promocode(promocode)

        response_data = {
            'order_number': order_id,
            'amount': amount,
            'cell_count': len(cells),
            'payment_message': payment_message,
            'promocode_used': bool(promocode_data),
            'promocode_discount': promocode_data['discount_cells'] if promocode_data else 0
        }
        
        print(f"✅ Order created: {response_data}")
        return jsonify(response_data)
        
    except Exception as e:
        print(f"❌ Error in buy_cells: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/check_promocode/<code>')
def check_promocode(code):
    print(f"🔍 Checking promocode: {code}")
    promocode = db.get_promocode(code)
    if not promocode:
        print(f"❌ Promocode {code} not found")
        return jsonify({'valid': False, 'error': 'Promocode not found'})
    
    if promocode['uses_left'] <= 0:
        print(f"❌ Promocode {code} has no uses left")
        return jsonify({'valid': False, 'error': 'No uses left'})
    
    print(f"✅ Promocode {code} valid, {promocode['uses_left']} uses left")
    return jsonify({
        'valid': True,
        'code': promocode['code'],
        'uses_left': promocode['uses_left'],
        'discount_cells': promocode['discount_cells']
    })

@app.route('/api/check_payment/<order_id>')
def check_payment(order_id):
    print(f"🔍 Checking payment for order: {order_id}")
    order_data = db.get_order(order_id)
    if not order_data:
        print(f"❌ Order {order_id} not found")
        return jsonify({'error': 'Order not found'}), 404

    order_id, cells_data_json, amount, status, promocode = order_data
    
    print(f"✅ Order {order_id} status: {status}")
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
    # При подключении отправляем все текущие пиксели
    pixels = db.get_all_pixels()
    socketio_app.emit('initial_pixels', {'pixels': pixels})
    print(f"📦 Sent {len(pixels)} initial pixels to new client")

@socketio_app.on('disconnect')
def handle_disconnect():
    print('🔴 Client disconnected')

# === ЗАПУСК ===
def start_da_connection():
    time.sleep(2)
    connect_to_donationalerts()
    
@app.route('/robots.txt')
def robots():
    return """User-agent: *
Disallow: /admin
Disallow: /api
Allow: /
""", 200, {'Content-Type': 'text/plain'}

import os
if __name__ == '__main__':
    print("🚀 Starting EmojiDesk Server...")
    threading.Thread(target=start_da_connection, daemon=True).start()
    port = int(os.environ.get('PORT', 5000))
    print(f"🌐 Server running on port {port}")
    socketio_app.run(app, debug=True, host='0.0.0.0', port=port, allow_unsafe_werkzeug=True)

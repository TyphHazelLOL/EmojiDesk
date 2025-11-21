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
        self.pixels = {}
        self.orders = {}
        self.promocodes = {
            'promocodena18rubley': {
                'uses_left': 3, 
                'max_uses': 3, 
                'discount_cells': 18
            }
        }
        print("✅ База данных инициализирована")
    
    def get_pixel(self, x, y):
        return self.pixels.get((x, y))
    
    def set_pixel(self, x, y, emoji, username, order_id=None):
        self.pixels[(x, y)] = {
            'emoji': emoji,
            'username': username,
            'order_id': order_id,
            'timestamp': datetime.now()
        }
        
        # Отправляем всем клиентам
        socketio_app.emit('pixel_update', {
            'x': x, 
            'y': y, 
            'emoji': emoji, 
            'username': username
        }, broadcast=True)
    
    def get_all_pixels(self):
        result = []
        for (x, y), data in self.pixels.items():
            result.append({
                'x': x, 
                'y': y, 
                'emoji': data['emoji'], 
                'username': data['username']
            })
        return result
    
    def create_order(self, cells_data, amount, promocode=None):
        order_id = str(uuid.uuid4())[:8]
        self.orders[order_id] = {
            'cells_data': cells_data,
            'amount': amount,
            'status': 'pending',
            'promocode': promocode,
            'created_at': datetime.now()
        }
        return order_id
    
    def get_order(self, order_id):
        order = self.orders.get(order_id)
        if order:
            return (order_id, json.dumps(order['cells_data']), order['amount'], order['status'], order['promocode'])
        return None
    
    def update_order_status(self, order_id, status):
        if order_id in self.orders:
            self.orders[order_id]['status'] = status
    
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
            return True
        return False

db = SimpleDatabase()

# === ОБРАБОТКА ДОНОВ ===
def process_donation_message(username, real_amount, order_id):
    order_data = db.get_order(order_id)
    if not order_data:
        return False

    order_id, cells_data_json, order_amount, status, promocode = order_data
    cells_data = json.loads(cells_data_json)

    if promocode:
        promocode_data = db.get_promocode(promocode)
        if promocode_data:
            for cell in cells_data:
                db.set_pixel(cell['x'], cell['y'], cell['emoji'], username, order_id)
            db.update_order_status(order_id, 'confirmed')
            return True
    else:
        if real_amount >= order_amount:
            for cell in cells_data:
                db.set_pixel(cell['x'], cell['y'], cell['emoji'], username, order_id)
            db.update_order_status(order_id, 'confirmed')
            return True
        else:
            db.update_order_status(order_id, 'rejected')
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
        promocode = data.get('promocode', '').strip()
        
        if not cells:
            return jsonify({'error': 'Выберите клетки'}), 400

        for cell in cells:
            if db.get_pixel(cell['x'], cell['y']):
                return jsonify({'error': f'Клетка ({cell["x"]},{cell["y"]}) уже занята'}), 400

        promocode_data = None
        if promocode:
            promocode_data = db.get_promocode(promocode)
            if not promocode_data:
                return jsonify({'error': 'Неверный промокод'}), 400
            if promocode_data['uses_left'] <= 0:
                return jsonify({'error': 'Промокод использован'}), 400
            if len(cells) != promocode_data['discount_cells']:
                return jsonify({'error': f'Нужно выбрать ровно {promocode_data["discount_cells"]} клеток'}), 400

        amount = 0.0 if promocode_data else len(cells) * 1.0

        order_id = db.create_order(cells, amount, promocode if promocode_data else None)

        if promocode_data:
            db.use_promocode(promocode)

        return jsonify({
            'order_number': order_id,
            'amount': amount,
            'cell_count': len(cells),
            'payment_message': f"order_{order_id}",
            'promocode_used': bool(promocode_data),
            'promocode_discount': promocode_data['discount_cells'] if promocode_data else 0
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/check_promocode/<code>')
def check_promocode(code):
    promocode = db.get_promocode(code)
    if not promocode:
        return jsonify({'valid': False, 'error': 'Промокод не найден'})
    
    if promocode['uses_left'] <= 0:
        return jsonify({'valid': False, 'error': 'Промокод использован'})
    
    return jsonify({
        'valid': True,
        'code': promocode['code'],
        'uses_left': promocode['uses_left'],
        'discount_cells': promocode['discount_cells']
    })

@app.route('/api/check_payment/<order_id>')
def check_payment(order_id):
    order_data = db.get_order(order_id)
    if not order_data:
        return jsonify({'error': 'Заказ не найден'}), 404

    order_id, cells_data_json, amount, status, promocode = order_data
    
    return jsonify({
        'status': status,
        'order_id': order_id,
        'amount': amount,
        'promocode_used': bool(promocode)
    })

# === SOCKET.IO ===
@socketio_app.on('connect')
def handle_connect():
    socketio_app.emit('initial_pixels', {'pixels': db.get_all_pixels()})

@socketio_app.on('disconnect')
def handle_disconnect():
    pass

# === ЗАПУСК ===
import os
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    socketio_app.run(app, debug=True, host='0.0.0.0', port=port, allow_unsafe_werkzeug=True)

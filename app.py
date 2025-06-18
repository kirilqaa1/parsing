from flask import Flask, request, render_template, redirect, url_for, session, flash
import os
import requests
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
import csv
import time
import threading
import unidecode
import glob
import re
import sqlite3
import random
from email.mime.text import MIMEText
import smtplib
import logging
import json
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from webdriver_manager.chrome import ChromeDriverManager
from flask import jsonify



app = Flask(__name__)
 
app.secret_key = 'supersecretkey'
@app.template_filter('extract_warehouses')
def extract_warehouses(stock_str):
    # Ищет коды типа DE1, PP1 и т.п.
    if not stock_str:
        return ''
    found = re.findall(r'\b[A-Z]{2}\d\b', stock_str)
    return 'Наличие на складах: ' + ', '.join(found) if found else ''

logging.basicConfig(
    filename='repricer.log',
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    encoding='utf-8'
)

import sqlite3

def init_db():
    conn = sqlite3.connect('users.db')
    c = conn.cursor()

    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            is_verified INTEGER DEFAULT 0,
            verification_code TEXT,
            paid_until TEXT,
            plan_id INTEGER DEFAULT 1,
            trial_until TEXT
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            sku_limit INTEGER NOT NULL,
            price INTEGER NOT NULL
        )
    ''')

    c.execute("SELECT COUNT(*) FROM plans")
    if c.fetchone()[0] == 0:
        c.executemany('''
            INSERT INTO plans (name, sku_limit, price) VALUES (?, ?, ?)
        ''', [
            ("До 50 SKU", 50, 20000),
            ("До 200 SKU", 200, 35000),
            ("До 500 SKU", 500, 60000)
        ])

    # 👇 Вставляем аккуратно с нужными отступами
    c.execute('''
        CREATE TABLE IF NOT EXISTS payroll_months (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            register_email TEXT NOT NULL,
            month TEXT NOT NULL,
            total INTEGER DEFAULT 0
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS payroll_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            month_id INTEGER NOT NULL,
            fio TEXT,
            payment_type TEXT,
            oklad INTEGER,
            days INTEGER,
            bonus INTEGER,
            vacation INTEGER,
            sick INTEGER,
            total INTEGER,
            FOREIGN KEY (month_id) REFERENCES payroll_months(id)
        )
    ''')

    conn.commit()
    conn.close()

init_db()

import sqlite3

conn = sqlite3.connect('users.db')
c = conn.cursor()
c.execute("PRAGMA table_info(users)")
columns = [col[1] for col in c.fetchall()]
if 'trial_until' not in columns:
    c.execute('ALTER TABLE users ADD COLUMN trial_until TEXT')
conn.commit()
conn.close()
driver = None
driver_parser = None
driver_changer_lock = threading.Lock()
driver_changer = None
repricer_active = False
last_products = []
repricer_running = False
repricer_timer = None

def get_user_folder():
    register_email = session.get('register_email')
    kaspi_email = session.get('kaspi_email')
    if not register_email or not kaspi_email:
        return None
    folder = os.path.join('userdata', register_email, kaspi_email)
    os.makedirs(folder, exist_ok=True)
    return folder



def get_user_upload_folder():
    folder = get_user_folder()
    if not folder:
        return None
    upload_folder = os.path.join(folder, 'uploads')
    os.makedirs(upload_folder, exist_ok=True)
    return upload_folder

@app.route('/login', methods=['GET', 'POST'])
def login_user():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']

        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        c.execute('SELECT password, is_verified FROM users WHERE email = ?', (email,))
        row = c.fetchone()
        conn.close()

        if row:
            db_password, is_verified = row
            if password == db_password:
                if is_verified:
                    session['register_email'] = email
                    flash('Успешный вход', 'success')

                    # ✅ Восстановление подключённого магазина
                    try:
                        user_folder = os.path.join('userdata', email)
                        if not os.path.exists(user_folder):
                            raise Exception("Папка пользователя не найдена")

                        kaspi_folders = os.listdir(user_folder)
                        if not kaspi_folders:
                            raise Exception("Не найдено ни одного магазина")

                        kaspi_email = kaspi_folders[0]  # берём первый магазин
                        session['kaspi_email'] = kaspi_email

                        store_path = os.path.join(user_folder, kaspi_email, 'store.json')
                        with open(store_path, encoding='utf-8') as f:
                            store_data = json.load(f)
                            session['store_name'] = store_data.get('store')
                            session['kaspi_password'] = store_data.get('kaspi_password')

                    except Exception as e:
                        print(f"[!] Ошибка загрузки магазина: {e}")

                    return redirect(url_for('settings'))

                else:
                    flash('Подтвердите email перед входом', 'error')
                    return redirect(url_for('verify'))

        flash('Неверный email или пароль', 'error')

    return render_template('login.html')




def get_user_trial_until(email):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('SELECT trial_until FROM users WHERE email = ?', (email,))
    row = c.fetchone()
    conn.close()
    if row and row[0]:
        return datetime.strptime(row[0], '%Y-%m-%d %H:%M:%S')
    return None


from datetime import datetime

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    store_name = None
    api_token = None
    repricer_enabled = False
    global repricer_running

    if request.method == 'POST':
        kaspi_email = request.form['kaspi_email']
        kaspi_password = request.form['kaspi_password']
        session['kaspi_email'] = kaspi_email
        session['kaspi_password'] = kaspi_password

        try:
            options = Options()
            # options.add_argument("--headless")
            options.add_argument("--disable-blink-features=AutomationControlled")
            driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
            wait = WebDriverWait(driver, 5)

            driver.get("https://idmc.shop.kaspi.kz/login")
            wait.until(EC.presence_of_element_located((By.ID, "user_email_field"))).send_keys(kaspi_email)
            wait.until(EC.element_to_be_clickable((By.CLASS_NAME, "button"))).click()
            wait.until(EC.presence_of_element_located((By.ID, "password_field"))).send_keys(kaspi_password)
            wait.until(EC.element_to_be_clickable((By.CLASS_NAME, "button"))).click()
            wait.until(EC.url_contains("kaspi.kz/mc"))
            time.sleep(1)

            driver.get("https://kaspi.kz/mc/#/settings")
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, '[data-testid="merchant-name"]')))
            store_name = driver.find_element(By.CSS_SELECTOR, '[data-testid="merchant-name"]').text.strip()
            time.sleep(1)

            driver.get("https://kaspi.kz/mc/#/settings?activeTab=5")
            time.sleep(1)
            try:
                input_el = driver.find_element(By.CSS_SELECTOR, '.token__field input.input')
                api_token = input_el.get_attribute("value")
            except:
                api_token = ''

            folder = get_user_folder()
            os.makedirs(folder, exist_ok=True)
            store_path = os.path.join(folder, "store.json")

            store_data = {
                "store": store_name,
                "kaspi_email": kaspi_email,
                "kaspi_password": kaspi_password,
                "api_token": api_token,
                "repricer_enabled": True,
                "repricer_running": repricer_running
            }

            with open(store_path, "w", encoding="utf-8") as f:
                json.dump(store_data, f, ensure_ascii=False, indent=2)

            session['store_name'] = store_name
            flash("Магазин успешно подключён", "success")

        except Exception as e:
            flash(f"Ошибка подключения: {e}", "error")
        finally:
            driver.quit()

    folder = get_user_folder()
    if folder:
        try:
            store_path = os.path.join(folder, "store.json")
            with open(store_path, encoding="utf-8") as f:
                store_data = json.load(f)
                store_name = store_data.get("store", "")
                api_token = store_data.get("api_token", "")
                repricer_enabled = store_data.get("repricer_enabled", False)
                repricer_running = store_data.get("repricer_running", False)
        except Exception as e:
            flash(f"Ошибка загрузки настроек: {e}", "error")

    # 💡 Подписка
    sku_count = count_user_skus()
    subscription_price = get_subscription_price(sku_count)
    paid_until = get_paid_until(session['register_email'])
    plan_name = get_plan_name(session['register_email'])
    trial_until = get_user_trial_until(session['register_email'])
    current_time = datetime.now()

    if subscription_price == -1:
        subscription_active = False
    else:
        subscription_active = is_user_paid()

    return render_template('store_settings.html',
        store_name=store_name,
        api_token=api_token,
        repricer_running=repricer_running,
        subscription_active=subscription_active,
        subscription_price=subscription_price,
        sku_count=sku_count,
        paid_until=paid_until,
        plan_name=plan_name,
        trial_until=trial_until,
        current_time=current_time
    )

import glob

def get_paid_until(register_email):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute("SELECT paid_until FROM users WHERE email = ?", (register_email,))
    result = c.fetchone()
    conn.close()
    return result[0] if result and result[0] else None


def get_plan_name(register_email):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('''
        SELECT plans.name
        FROM users
        JOIN plans ON users.plan_id = plans.id
        WHERE users.email = ?
    ''', (register_email,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else "Без тарифа"

@app.route('/index', methods=['GET', 'POST'])
def index():
    # 🔒 Проверка авторизации
    if 'register_email' not in session:
        return redirect(url_for('login_user'))

    register_email = session.get('register_email')
    kaspi_email = session.get('kaspi_email')
    store_name = ""

    # 🔒 Проверка подключения Kaspi-магазина
    if not kaspi_email:
        flash('Сначала подключите Kaspi-магазин', 'error')
        return redirect(url_for('settings'))

    # 🔠 Загрузка имени магазина из store.json
    try:
        store_path = os.path.join("userdata", register_email, kaspi_email, "store.json")
        with open(store_path, encoding="utf-8") as f:
            store_name = json.load(f).get("store", "").strip()
            session['store_name'] = store_name
    except:
        store_name = ""

    global last_products
    last_products = []
    user_folder = get_user_folder()
    json_path = os.path.join(user_folder, 'last_products.json')

    if os.path.exists(json_path):
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                last_products = json.load(f)
        except Exception as e:
            flash(f"Ошибка при загрузке last_products.json: {e}", "error")

    # 📄 Постраничный вывод
    page = int(request.args.get('page', 1))
    per_page = 20
    total = len(last_products)
    pages = (total + per_page - 1) // per_page
    start = (page - 1) * per_page
    end = start + per_page
    paginated_products = last_products[start:end]

    return render_template(
        'index.html',
        uploaded=bool(last_products),
        products=paginated_products,
        page=page,
        pages=pages,
        repricer_running=repricer_running,
        store_name=store_name,
        kaspi_connected=True
    )

@app.route('/restore_to_sale', methods=['POST'])
def restore_to_sale():
    global last_products
    sku_count = count_user_skus()
    price = get_subscription_price(sku_count)

    if price == -1:
        return jsonify({'error': 'Превышен лимит SKU. Свяжитесь с поддержкой.'}), 403

    if price > 0 and not is_user_paid():
        return jsonify({'error': f'Для {sku_count} SKU требуется подписка {price}₸/мес. Доступ заблокирован.'}), 403
    email = session.get('kaspi_email')
    password = session.get('kaspi_password')
    data = request.get_json()
    skus = data.get('skus', [])

    options = Options()
    # options.add_argument("--headless")
    options.add_argument('--disable-blink-features=AutomationControlled')
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    wait = WebDriverWait(driver, 20)

    try:
        driver.get("https://idmc.shop.kaspi.kz/login")
        wait.until(EC.presence_of_element_located((By.ID, "user_email_field"))).send_keys(email)
        wait.until(EC.element_to_be_clickable((By.CLASS_NAME, "button"))).click()
        wait.until(EC.presence_of_element_located((By.ID, "password_field"))).send_keys(password)
        wait.until(EC.element_to_be_clickable((By.CLASS_NAME, "button"))).click()
        wait.until(EC.url_contains("kaspi.kz/mc"))
        time.sleep(2)

        for sku in skus:
            try:
                driver.get(f"https://kaspi.kz/mc/#/offer/{sku}")
                wait.until(EC.presence_of_element_located((By.XPATH, '//button[.//span[contains(text(), "Выставить на продажу")]]')))
                btn = driver.find_element(By.XPATH, '//button[.//span[contains(text(), "Выставить на продажу")]]')
                btn.click()

                time.sleep(2)
                wait.until(EC.presence_of_element_located((By.XPATH, '//button[.//span[contains(text(), "Сохранить изменения")]]')))
                save_btn = driver.find_element(By.XPATH, '//button[.//span[contains(text(), "Сохранить изменения")]]')
                save_btn.click()
                time.sleep(2)  #

                for p in last_products:
                    if p['sku'] == sku:
                        p['removed'] = False
                        product = p
                        break

                # Добавим обратно в XML, если отсутствует
                try:
                    xml_path = os.path.join(get_user_upload_folder(), 'active.xml')
                    tree = ET.parse(xml_path)
                    root = tree.getroot()
                    ns = {'k': root.tag.split('}')[0].strip('{')}
                    offers = root.find('k:offers', ns)
                    exists = any(offer.get('sku') == sku for offer in offers.findall('k:offer', ns))
                    if not exists:
                        offer_el = ET.Element(f"{{{ns['k']}}}offer", sku=sku)
                        model_el = ET.SubElement(offer_el, f"{{{ns['k']}}}model")
                        model_el.text = product['model']
                        price_el = ET.SubElement(offer_el, f"{{{ns['k']}}}cityprice", cityId="551010000")
                        price_el.text = str(product['price'])
                        offers.append(offer_el)
                        tree.write(xml_path, encoding='utf-8', xml_declaration=True)
                except Exception as e:
                    logging.warning(f"[!] Не удалось добавить товар в XML после выставления: {e}")

            except Exception as e:
                print(f"Ошибка при выставлении SKU {sku}: {e}")

        # 💾 Сохраняем обновлённый last_products
        try:
            user_folder = get_user_folder()
            json_path = os.path.join(user_folder, 'last_products.json')
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(last_products, f, ensure_ascii=False, indent=2)
            for p in last_products:
                if 'selected' in p:
                    del p['selected']

        except Exception as e:
            logging.warning(f"[!] Ошибка сохранения после выставления: {e}")

    finally:
        driver.quit()

    return jsonify({'status': 'ok'})



@app.route('/download_xml', methods=['POST'])
def download_xml():
    from flask import session, flash, redirect, url_for
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from webdriver_manager.chrome import ChromeDriverManager
    from threading import Thread, Lock
    from queue import Queue
    import time, os, re, json
    from math import ceil

    email = session.get('kaspi_email')
    password = session.get('kaspi_password')
    register_email = session.get('register_email')
    kaspi_email = session.get('kaspi_email')

    if not email or not password or not register_email or not kaspi_email:
        flash("Сначала войдите и подключите магазин", "error")
        return redirect(url_for("login_user"))

    MAX_TABS = 5
    NUM_PARSER_THREADS = 3
    card_queue = Queue()
    page_queue = Queue()
    result_data = []
    result_lock = Lock()

    def get_chrome_options():
        options = Options()
        options.add_argument("--headless")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-blink-features=AutomationControlled")
        return options

    def kaspi_login(driver):
        wait = WebDriverWait(driver, 15)
        driver.get("https://idmc.shop.kaspi.kz/login")
        wait.until(EC.presence_of_element_located((By.ID, "user_email_field"))).send_keys(email)
        driver.find_element(By.CSS_SELECTOR, "button.button.is-primary").click()
        wait.until(EC.presence_of_element_located((By.ID, "password_field"))).send_keys(password)
        driver.find_element(By.CSS_SELECTOR, "button.button.is-primary").click()
        wait.until(EC.url_contains("orders?status=NEW"))

    def discover_and_enqueue_pages():
        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=get_chrome_options())
        kaspi_login(driver)
        driver.get("https://kaspi.kz/mc/#/products/active/1")
        time.sleep(2.5)

        try:
            info_el = driver.find_element(By.CSS_SELECTOR, ".page-info")
            text = info_el.text.strip()  # "1 - 10 из 197"
            total = int(re.search(r"из\s+(\d+)", text).group(1))
            pages = ceil(total / 10)
            print(f"[✓] Всего страниц: {pages}")

            for i in range(1, pages + 1):
                page_queue.put(i)

        except Exception as e:
            print(f"[X] Не удалось определить количество страниц: {e}")

        driver.quit()

    def parser_worker_pages():
        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=get_chrome_options())
        kaspi_login(driver)

        while not page_queue.empty():
            page = page_queue.get()
            url = f"https://kaspi.kz/mc/#/products/active/{page}"
            print(f"[⇨] Загружаем страницу {page}")
            driver.get(url)

            try:
                WebDriverWait(driver, 8).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "div.card-content"))
                )
            except:
                print(f"[!] Не найдены карточки на странице {page}, пропуск")
                page_queue.task_done()
                continue

            products = driver.find_elements(By.CSS_SELECTOR, "div.card-content")
            print(f"[📦] Найдено карточек: {len(products)} на стр. {page}")

            for p in products:
                try:
                    title = p.find_element(By.CSS_SELECTOR, "p.is-5 a").text.strip()
                    subtitle_html = p.find_element(By.CSS_SELECTOR, "p.subtitle.is-6").get_attribute("innerHTML")
                    raw_lines = re.split(r'<br\s*/?>', subtitle_html)
                    clean_lines = [re.sub(r'<[^>]+>', '', line).strip() for line in raw_lines if line.strip()]
                    article = "-"
                    if len(clean_lines) >= 2:
                        pre_last_line = clean_lines[-2]
                        match = re.search(r"\d{6,}(?:_\d+)?", pre_last_line)
                        if match:
                            article = match.group(0)

                    img = p.find_element(By.CSS_SELECTOR, "img.thumbnail").get_attribute("src")
                    try:
                        price_el = p.find_element(By.XPATH, ".//following::p[contains(@class, 'subtitle') and contains(@class, 'is-5')]")
                        price = price_el.text.strip().replace("₸", "").replace(" ", "")
                    except:
                        price = "-"

                    if article != "-":
                        card_queue.put({
                            "название": title,
                            "артикул": article,
                            "цена": price,
                            "изображение": img,
                            "ссылка": f"https://kaspi.kz/mc/#/offer/{article}"
                        })

                except Exception as e:
                    print(f"[!] Пропущен товар на стр. {page}: {e}")

            page_queue.task_done()

        driver.quit()


    def offer_worker():
        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=get_chrome_options())
        kaspi_login(driver)
        wait = WebDriverWait(driver, 10)

        counter = 0

        while not card_queue.empty():
            card = card_queue.get()
            counter += 1

            # ⏳ Добавим задержку на первые 2 карточки
            if counter <= 2:
                time.sleep(2.5)

            try:
                driver.get(card["ссылка"])
                wait.until(EC.presence_of_element_located((By.CLASS_NAME, "product-info-field")))
                info_fields = driver.find_elements(By.CLASS_NAME, "product-info-field")

                for field in info_fields:
                    name_el = field.find_element(By.CLASS_NAME, "product-info-field-name")
                    value_el = field.find_element(By.CLASS_NAME, "product-info-field-value")
                    name = name_el.text.strip()
                    value = value_el.text.strip()
                    if name == "Категория":
                        card["категория"] = value
                    elif name == "Бренд":
                        card["бренд"] = value
                    elif name == "Наличие на складах":
                        try:
                            pp_title = value_el.find_element(By.CLASS_NAME, "availability-modal__point-title").text.strip()
                            card["наличие"] = pp_title
                        except:
                            card["наличие"] = "–"

                try:
                    link_el = driver.find_element(By.CSS_SELECTOR, "a[href*='/shop/p/']")
                    href = link_el.get_attribute("href")
                    card["ссылка_kaspi"] = href if href.startswith("http") else f"https://kaspi.kz{href}"
                except:
                    card["ссылка_kaspi"] = "–"

            except Exception as e:
                print(f"[!] Ошибка при обработке {card.get('артикул')}: {e}")
                card["категория"] = card["бренд"] = card["наличие"] = card["ссылка_kaspi"] = "–"

            with result_lock:
                result_data.append(card)
            card_queue.task_done()

            try:
                driver.get("https://kaspi.kz/mc/#/products/active/1")
            except Exception as e:
                print(f"[!] Ошибка возврата на /products/active/1: {e}")

        driver.quit()

    # Шаг 1 — определяем количество страниц
    discover_and_enqueue_pages()

    # Шаг 2 — запускаем парсинг страниц
    parser_threads = []
    for _ in range(NUM_PARSER_THREADS):
        t = Thread(target=parser_worker_pages)
        t.start()
        parser_threads.append(t)
    for t in parser_threads:
        t.join()

    # Шаг 3 — обрабатываем карточки
    offer_threads = []
    for _ in range(MAX_TABS):
        t = Thread(target=offer_worker)
        t.start()
        offer_threads.append(t)
    card_queue.join()
    for t in offer_threads:
        t.join()

    # Сохраняем результат
    folder = os.path.join("userdata", register_email, kaspi_email)
    os.makedirs(folder, exist_ok=True)
    json_path = os.path.join(folder, "last_products.json")

    seen_skus = set()
    last_products = []
    for card in result_data:
        sku = card.get("артикул")
        if sku and sku not in seen_skus:
            seen_skus.add(sku)
            last_products.append({
                "sku": sku,
                "model": card.get("название", ""),
                "price": card.get("цена", ""),
                "link": card.get("ссылка_kaspi", ""),
                "image": card.get("изображение", ""),
                "category": card.get("категория", ""),
                "brand": card.get("бренд", ""),
                "stock": card.get("наличие", ""),
                "first_price": "",
                "position": "",
                "step": "",
                "min": "",
                "max": "",
                "selected": False,
                "auto_down": False,
                "auto_up": False,
                "removed": False
            })

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(last_products, f, ensure_ascii=False, indent=2)

    flash(f"✅ Загружено товаров: {len(last_products)}", "success")
    return redirect(url_for("index"))




def fetch_model_info_from_offer(sku, driver, wait):
    model_name = ''
    product_link = ''
    image_url = ''

    try:

        driver.get(f"https://kaspi.kz/mc/#/products/active/1")
        time.sleep(1)
        driver.get(f"https://kaspi.kz/mc/#/offer/{sku}")
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".master-product__title")))

        # Модель
        model_el = driver.find_element(By.CSS_SELECTOR, ".master-product__title")
        model_name = model_el.text.strip()

        # Ссылка на товар
        a_tags = driver.find_elements(By.TAG_NAME, "a")
        for a in a_tags:
            href = a.get_attribute("href")
            if href and "/shop/p/" in href:
                product_link = "https://kaspi.kz" + href if href.startswith("/shop") else href
                break

        # Картинка — ищем первую <img> с "media-view__image"
        try:
            image_el = driver.find_element(By.CSS_SELECTOR, "img.media-view__image")
            image_url = image_el.get_attribute("src")
        except:
            pass

            # 💾 Сохраняем обновлённый last_products.json
        try:
            user_folder = get_user_folder()
            if user_folder:
                json_path = os.path.join(user_folder, 'last_products.json')
                with open(json_path, 'w', encoding='utf-8') as f:
                    json.dump(last_products, f, ensure_ascii=False, indent=2)
                logging.info(f"[✔] last_products.json сохранён: {json_path}")
            else:
                logging.warning("[!] user_folder не найден, не удалось сохранить last_products")
        except Exception as e:
            logging.error(f"[X] Ошибка при сохранении last_products в fill_missing_models_from_kaspi: {e}")


        return model_name, product_link, image_url

    except Exception as e:
        logging.warning(f"[!] Ошибка при получении инфы по SKU {sku}: {e}")
        return '', '', ''
    



def get_model_cache_path():
    register_email = session.get('register_email')
    kaspi_email = session.get('kaspi_email')
    if not register_email or not kaspi_email:
        return None
    return os.path.join("userdata", register_email, kaspi_email, "model_cache.json")


def load_model_cache():
    path = get_model_cache_path()
    if path and os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_model_cache(cache):
    path = get_model_cache_path()
    if path:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)

@app.route('/logout')
def logout():
    session.clear()
    flash('Вы вышли из аккаунта сайта', 'info')
    return redirect(url_for('landing'))


@app.route('/start_repricer', methods=['POST'])
def start_repricer():
    register_email = session.get('register_email')
    kaspi_email = session.get('kaspi_email')
    password = session.get('kaspi_password')

    # 🛡 Проверка подписки перед запуском
    if not is_user_paid():
        flash('⛔ Подписка не активна или истёк пробный период', 'error')
        return redirect(url_for('index'))

    # ✅ запуск репрайсера в отдельном потоке
    threading.Thread(
        target=run_repricer_loop,
        args=(register_email, kaspi_email, password),
        daemon=True
    ).start()

    flash('Репрайсер запущен в фоне', 'success')
    return redirect(url_for('index'))


def slugify(text):
    text = unidecode.unidecode(text).lower()
    text = re.sub(r'\s+', '-', text)
    text = re.sub(r'[^a-z0-9\-]', '', text)
    return text.strip('-')

import re

def normalize(name: str) -> str:
    """Нормализует имя продавца: нижний регистр, удаление всех не-букв/цифр"""
    name = name.lower()
    name = re.sub(r"[^a-zа-я0-9]", "", name)
    return name

def parse_competitor_price(product_code, register_email, kaspi_email, password):
    global driver_parser
    try:
        json_path = os.path.join("userdata", register_email, kaspi_email, "last_products.json")
        if not os.path.exists(json_path):
            logging.warning(f"[!] last_products.json не найден")
            return None

        with open(json_path, "r", encoding="utf-8") as f:
            last_products = json.load(f)

        product = next((p for p in last_products if p.get("sku") == product_code), None)
        if not product:
            logging.warning(f"[!] SKU {product_code} не найден в last_products")
            return None

        product_link = product.get("link")
        if not product_link:
            logging.warning(f"[!] Нет ссылки у SKU {product_code}")
            return None

        options = Options()
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_argument("--headless")

        if not driver_parser:
            driver_parser = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
            driver_parser.maximize_window()

        driver_parser.get(product_link)
        time.sleep(1)

        try:
            close_button = driver_parser.find_element(By.CSS_SELECTOR, "i.icon.icon_close")
            close_button.click()
            time.sleep(0.5)
        except:
            pass

        my_store_raw = get_my_store_name(register_email, kaspi_email)
        my_store = normalize(my_store_raw)

        sellers = []
        rows = driver_parser.find_elements(By.CSS_SELECTOR, "table.sellers-table__self > tbody > tr")
        if not rows:
            logging.warning(f"[!] Продавцы не найдены для SKU {product_code}")
            return None

        for row in rows:
            try:
                name_el = row.find_element(By.CSS_SELECTOR, "td:nth-child(1) a")
                name_raw = name_el.text.strip()
                name = normalize(name_raw)

                price_el = row.find_element(By.CSS_SELECTOR, "td:nth-child(4) .sellers-table__price-cell-text")
                price_raw = price_el.text
                price_text = re.sub(r"[^\d]", "", price_raw)

                if not price_text:
                    continue

                price = int(price_text)
                sellers.append((name, price))
            except Exception as e:
                logging.warning(f"[!] Ошибка при парсинге продавца: {e}")

        if not sellers:
            logging.warning(f"[!] Продавцы есть, но цены не найдены: SKU {product_code}")
            return None

        sellers.sort(key=lambda x: x[1])
        competitor_min = sellers[0][1]

        my_price = None
        position = None
        for idx, (name, price) in enumerate(sellers, 1):
            if name == my_store:
                my_price = price
                position = idx
                break

        # 🧠 Обновляем данные прямо в last_products
        product["first_price"] = competitor_min
        product["position"] = position
        product["price"] = str(my_price) if my_price else product.get("price", "")

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(last_products, f, ensure_ascii=False, indent=2)

        if my_price is not None and my_price <= competitor_min:
            logging.info(f"[✓] Ваша цена уже минимальная: {my_price}₸ ≤ {competitor_min}₸")
            return None

        return competitor_min

    except Exception as e:
        logging.error(f"[X] Ошибка SKU {product_code}: {e}")
        return None



@app.route('/api/photo/<sku>')
def get_photo_for_sku(sku):
    email = session.get('kaspi_email')
    if not email:
        return {"image": ""}

    model_cache_path = os.path.join("userdata", email, "model_cache.json")
    if not os.path.exists(model_cache_path):
        return {"image": ""}

    with open(model_cache_path, 'r', encoding='utf-8') as f:
        cache = json.load(f)

    image_url = cache.get(sku, {}).get("image", "")
    return {"image": image_url}



def login_and_update_price(sku, new_price, email, password):
    global driver_changer
    try:
        if driver_changer is None:
            options = Options()
            options.add_argument("--disable-blink-features=AutomationControlled")
            driver_changer = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
            options.add_argument("--headless")
            driver_changer.maximize_window()

            wait = WebDriverWait(driver_changer, 20)

            # Авторизация
            driver_changer.get("https://idmc.shop.kaspi.kz/login")
            wait.until(EC.presence_of_element_located((By.ID, "user_email_field"))).send_keys(email)
            wait.until(EC.element_to_be_clickable((By.CLASS_NAME, "button"))).click()
            wait.until(EC.presence_of_element_located((By.ID, "password_field"))).send_keys(password)
            wait.until(EC.element_to_be_clickable((By.CLASS_NAME, "button"))).click()
            wait.until(EC.url_contains("kaspi.kz/mc"))
            time.sleep(1)
            logging.info(f"[✔] Авторизация driver_changer выполнена")

        # Переход сразу на SKU, без промежуточной /#/ страницы
        driver_changer.get(f"https://kaspi.kz/mc/#/offer/{sku}")
        wait = WebDriverWait(driver_changer, 3)

        # Кликаем по кнопке смены цены
        wait.until(EC.element_to_be_clickable((By.CLASS_NAME, "change-price-button"))).click()

        # Ожидаем поле ввода и отправляем цену
        input_field = wait.until(EC.presence_of_element_located((By.XPATH, "//input[@inputmode='numeric']")))
        input_field.clear()
        input_field.send_keys(str(new_price))

        # Кликаем по кнопке подтверждения
        wait.until(EC.element_to_be_clickable((
            By.XPATH, "//div[contains(@class,'modal')]//button[contains(@class,'is-primary')]"
        ))).click()

        logging.info(f"[✔] Цена обновлена: SKU {sku} → {new_price}₸")

    except Exception as e:
        logging.error(f"[X] Ошибка при обновлении цены для {sku}: {e}")


@app.route('/toggle_repricer', methods=['POST'])
def toggle_repricer():
    global repricer_running, repricer_timer

    sku_count = count_user_skus()  # ← сначала получаем количество SKU
    price = get_subscription_price(sku_count)

    if price == -1:
        return jsonify({'error': 'Превышен лимит SKU. Свяжитесь с поддержкой.'}), 403

    if price > 0 and not is_user_paid():
        return jsonify({'error': f'Для {sku_count} SKU требуется подписка {price}₸/мес. Доступ заблокирован.'}), 403
    register_email = session.get('register_email')
    email = session.get('kaspi_email')
    password = session.get('kaspi_password')

    repricer_running = not repricer_running
    sku_count = count_user_skus()
   
    # 🧠 Сохраняем состояние в store.json
    folder = get_user_folder()
    if folder:
        store_path = os.path.join(folder, "store.json")
        try:
            if os.path.exists(store_path):
                with open(store_path, "r", encoding="utf-8") as f:
                    store_data = json.load(f)
            else:
                store_data = {}
            store_data["repricer_running"] = repricer_running
            with open(store_path, "w", encoding="utf-8") as f:
                json.dump(store_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.warning(f"[!] Не удалось сохранить состояние репрайсера: {e}")

    if repricer_running:
        def loop():
            while repricer_running:
                run_repricer_loop(register_email, email, password)
                time.sleep(180)  # каждые 3 минуты
        repricer_timer = threading.Thread(target=loop, daemon=True)
        repricer_timer.start()
        return jsonify({'status': 'enabled'})
    else:
        return jsonify({'status': 'disabled'})



def login_driver_changer(register_email, kaspi_email, password):
    global driver_changer
    with driver_changer_lock:
        if driver_changer is not None:
            try:
                driver_changer.title
                return
            except:
                logging.warning("[!] driver_changer недоступен, переинициализация")
                try:
                    driver_changer.quit()
                except:
                    pass
                driver_changer = None

        try:
            options = Options()
            options.add_argument("--headless")
            options.add_argument("--disable-blink-features=AutomationControlled")
            driver_changer = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
            driver_changer.maximize_window()

            wait = WebDriverWait(driver_changer, 20)
            driver_changer.get("https://idmc.shop.kaspi.kz/login")

            wait.until(EC.presence_of_element_located((By.ID, "user_email_field"))).send_keys(kaspi_email)
            wait.until(EC.element_to_be_clickable((By.CLASS_NAME, "button"))).click()
            wait.until(EC.presence_of_element_located((By.ID, "password_field"))).send_keys(password)
            wait.until(EC.element_to_be_clickable((By.CLASS_NAME, "button"))).click()
            wait.until(EC.url_contains("kaspi.kz/mc"))

            logging.info("[✔] driver_changer вошёл и готов к смене цен")

        except Exception as e:
            logging.error(f"[X] Ошибка входа в driver_changer: {e}")
            driver_changer = None



def get_my_store_name(register_email, kaspi_email):
    if not register_email or not kaspi_email:
        return ""
    try:
        path = os.path.join("userdata", register_email, kaspi_email, "store.json")
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("store", "").strip()
    except:
        return ""





def run_repricer_loop(register_email, kaspi_email, password):
    global repricer_active, driver, driver_parser, driver_changer, last_products

    if not is_user_paid(register_email):
        logging.warning("[⛔] Репрайсер не запущен — нет подписки")
        return

    repricer_active = True
    login_driver_changer(register_email, kaspi_email, password)

    user_folder = os.path.join('userdata', register_email, kaspi_email)
    result_path = os.path.join(user_folder, 'result.csv')
    last_json_path = os.path.join(user_folder, 'last_products.json')

    if not os.path.exists(result_path):
        logging.error(f"[!] result.csv не найден: {result_path}")
        return

    if not os.path.exists(last_json_path):
        logging.error(f"[!] last_products.json не найден: {last_json_path}")
        return

    with open(last_json_path, encoding='utf-8') as f:
        last_products = json.load(f)

    try:
        price_changed = False
        my_store = get_my_store_name(register_email, kaspi_email)

        with open(result_path, newline='', encoding='utf-8') as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                try:
                    sku = row['SKU']
                    price = int(float(row['Price']))
                    step = int(row['Step']) if row['Step'].isdigit() else 0
                    min_price = int(row['Min']) if row['Min'].isdigit() else 0
                    max_price = int(row['Max']) if row['Max'].isdigit() else 9999999

                    auto_down = str(row.get('auto_down', 'true')).strip().lower() == 'true'
                    auto_up = str(row.get('auto_up', 'true')).strip().lower() == 'true'

                    # 🔍 ищем товар в last_products.json
                    product = next((p for p in last_products if p.get("sku") == sku), None)
                    if not product:
                        logging.warning(f"[!] SKU {sku} не найден в last_products.json")
                        continue

                    product["step"] = step
                    product["min"] = min_price
                    product["max"] = max_price
                    product["auto_down"] = auto_down
                    product["auto_up"] = auto_up

                    # 🔄 вызываем парсинг конкурентов
                    competitor_price = parse_competitor_price(sku, register_email, kaspi_email, password)
                    if competitor_price is None:
                        continue

                    # 🔁 пересчитываем last_products, чтобы увидеть first_price/position
                    with open(last_json_path, encoding='utf-8') as f:
                        last_products = json.load(f)

                    product = next((p for p in last_products if p.get("sku") == sku), None)
                    if not product:
                        continue

                    # 🟢 Повышение до min
                    if auto_up and price < min_price:
                        new_price = min_price
                        login_and_update_price(sku, new_price, kaspi_email, password)
                        product["price"] = str(new_price)
                        price_changed = True
                        logging.info(f"[↑] Цена поднята до min: {sku} {price}₸ → {new_price}₸")
                        continue

                    # 🔼 Повышение до конкурента
                    if auto_up and price < competitor_price and price < max_price:
                        new_price = min(competitor_price - step, max_price)
                        if new_price > price:
                            login_and_update_price(sku, new_price, kaspi_email, password)
                            product["price"] = str(new_price)
                            price_changed = True
                            logging.info(f"[↑] Цена повышена: {sku} {price}₸ → {new_price}₸ (конкурент: {competitor_price}₸)")
                        continue

                    # 🔽 Снижение до ниже конкурента
                    if auto_down and price > competitor_price:
                        candidate_price = competitor_price - 1
                        new_price = max(candidate_price, min_price)
                        if new_price < price:
                            login_and_update_price(sku, new_price, kaspi_email, password)
                            product["price"] = str(new_price)
                            price_changed = True
                            logging.info(f"[↓] Цена снижена: {sku} {price}₸ → {new_price}₸ (конкурент: {competitor_price}₸)")
                        else:
                            logging.info(f"[=] Не снижаем: результат {new_price}₸ ≥ текущей или < min")

                except Exception as inner_e:
                    logging.error(f"[!] Ошибка по SKU {row.get('SKU', 'UNKNOWN')}: {inner_e}")
                    continue


        if price_changed:
            with open(last_json_path, "w", encoding="utf-8") as f:
                json.dump(last_products, f, ensure_ascii=False, indent=2)
            logging.info("[✓] last_products.json обновлён с новыми ценами")

    except Exception as e:
        logging.error(f"[!] Ошибка в репрайсере: {e}")

    finally:
        for drv, name in [(driver_parser, "driver_parser"), (driver_changer, "driver_changer"), (driver, "driver")]:
            if drv:
                drv.quit()
                logging.info(f"[X] Закрыт {name}")
        driver_parser = driver_changer = driver = None
        repricer_active = False
        logging.info("[⛔] Репрайсер завершил работу")


@app.route('/payroll/<month>/export')
def export_payroll_excel(month):
    if 'register_email' not in session:
        return redirect(url_for('login_user'))

    conn = sqlite3.connect('users.db')
    c = conn.cursor()

    c.execute("SELECT id FROM payroll_months WHERE month = ? AND register_email = ?", (month, session['register_email']))
    row = c.fetchone()
    if not row:
        flash("Месяц не найден", "error")
        return redirect(url_for("payroll_home"))
    month_id = row[0]

    c.execute("""
        SELECT fio, payment_type, oklad, days, bonus, total
        FROM payroll_entries
        WHERE month_id = ?
    """, (month_id,))
    data = c.fetchall()
    conn.close()

    import io
    import xlsxwriter

    output = io.BytesIO()
    workbook = xlsxwriter.Workbook(output, {'in_memory': True})
    worksheet = workbook.add_worksheet("Зарплата")

    # Стили
    header_format = workbook.add_format({
        'bold': True, 'bg_color': '#F2F2F2', 'border': 1, 'align': 'center', 'valign': 'vcenter'
    })
    cell_format = workbook.add_format({'border': 1, 'align': 'left'})
    money_format = workbook.add_format({'border': 1, 'num_format': '#,##0₸', 'align': 'right'})

    headers = ["ФИО", "Вид выплаты", "Оклад", "Отработано дней", "Премия", "Сумма"]
    for col, h in enumerate(headers):
        worksheet.write(0, col, h, header_format)
        worksheet.set_column(col, col, 20)

    total_sum = 0
    for row_num, row in enumerate(data, 1):
        worksheet.write(row_num, 0, row[0], cell_format)
        worksheet.write(row_num, 1, row[1], cell_format)
        worksheet.write_number(row_num, 2, row[2], money_format)
        worksheet.write_number(row_num, 3, row[3], cell_format)
        worksheet.write_number(row_num, 4, row[4], money_format)
        worksheet.write_number(row_num, 5, row[5], money_format)
        total_sum += row[5]

    worksheet.write(row_num + 1, 4, "Итого:", header_format)
    worksheet.write(row_num + 1, 5, total_sum, money_format)

    workbook.close()
    output.seek(0)

    filename = f"Зарплата_{month}.xlsx"
    return send_file(output,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     as_attachment=True,
                     download_name=filename)


@app.route('/remove_from_sale', methods=['POST'])
def remove_from_sale():
    global last_products
    sku_count = count_user_skus()
    
    price = get_subscription_price(sku_count)

    if price == -1:
        return jsonify({'error': 'Превышен лимит SKU. Свяжитесь с поддержкой.'}), 403

    if price > 0 and not is_user_paid():
        return jsonify({'error': f'Для {sku_count} SKU требуется подписка {price}₸/мес. Доступ заблокирован.'}), 403
    email = session.get('kaspi_email')
    password = session.get('kaspi_password')
    data = request.get_json()
    skus = data.get('skus', [])

    options = Options()
    options.add_argument("--headless")
    options.add_argument('--disable-blink-features=AutomationControlled')
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    wait = WebDriverWait(driver, 20)

    try:
        driver.get("https://idmc.shop.kaspi.kz/login")
        wait.until(EC.presence_of_element_located((By.ID, "user_email_field"))).send_keys(email)
        wait.until(EC.element_to_be_clickable((By.CLASS_NAME, "button"))).click()
        wait.until(EC.presence_of_element_located((By.ID, "password_field"))).send_keys(password)
        wait.until(EC.element_to_be_clickable((By.CLASS_NAME, "button"))).click()
        wait.until(EC.url_contains("kaspi.kz/mc"))
        time.sleep(2)
        for sku in skus:
            try:
                driver.get(f"https://kaspi.kz/mc/#/offer/{sku}")
                wait.until(EC.presence_of_element_located((By.XPATH, '//button[.//span[contains(text(), "Снять с продажи")]]')))
                btn = driver.find_element(By.XPATH, '//button[.//span[contains(text(), "Снять с продажи")]]')
                btn.click()
                time.sleep(2)

                for p in last_products:
                    if p['sku'] == sku:
                        p['removed'] = True
                        break

            except Exception as e:
                print(f"Ошибка при снятии с продажи SKU {sku}: {e}")

        # Удаляем из XML
        xml_path = os.path.join(get_user_upload_folder(), 'active.xml')
        tree = ET.parse(xml_path)
        root = tree.getroot()
        ns = {'k': root.tag.split('}')[0].strip('{')}
        offers = root.find('k:offers', ns)

        for sku in skus:
            for offer in offers.findall('k:offer', ns):
                if offer.get('sku') == sku:
                    offers.remove(offer)
                    break

        tree.write(xml_path, encoding='utf-8', xml_declaration=True)

        # Сохраняем last_products с обновлённым removed
        try:
            user_folder = get_user_folder()
            json_path = os.path.join(user_folder, 'last_products.json')
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(last_products, f, ensure_ascii=False, indent=2)
            for p in last_products:
                if 'selected' in p:
                    del p['selected']

        except Exception as e:
            logging.warning(f"[!] Ошибка сохранения last_products после снятия: {e}")

    finally:
        driver.quit()

    return jsonify({'status': 'ok'})


@app.route('/save', methods=['POST'])
def save():
    global last_products

    # фильтруем товары с заполненным шагом и хотя бы одним флагом авто
    filtered = [
        p for p in last_products
        if str(p.get('step')).strip() != '' and (p.get('auto_down') or p.get('auto_up'))
    ]

    user_folder = get_user_folder()
    result_path = os.path.join(user_folder, 'result.csv')

    with open(result_path, 'w', newline='', encoding='utf-8') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(['SKU', 'Model', 'Price', 'Step', 'Min', 'Max', 'auto_down', 'auto_up'])
        for p in filtered:
            writer.writerow([
                p['sku'],
                p['model'],
                p['price'],
                p.get('step', ''),
                p.get('min', ''),
                p.get('max', ''),
                p.get('auto_down', True),
                p.get('auto_up', True),
            ])

    return ('', 204)


@app.route('/api/update_field', methods=['POST'])
def update_field():
    data = request.get_json()
    sku = str(data.get('sku')).strip()
    key = data.get('key')
    value = data.get('value')

    # 🔄 Загружаем last_products из файла
    user_folder = get_user_folder()
    json_path = os.path.join(user_folder, 'last_products.json')

    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            last_products = json.load(f)
    except Exception as e:
        print("[ERROR] Не удалось загрузить last_products:", e)
        return jsonify({'error': 'Не удалось загрузить данные'}), 500

    print("Все SKU в базе:", [repr(p.get('sku')) for p in last_products])

    # === СОЗДАНИЕ НОВОГО ТОВАРА ===
    if key == "create":
        try:
            payload = json.loads(value)
            cost_price = payload.get("cost_price", "")
            try:
                cost_price = float(str(cost_price).strip()) if cost_price != "" else ""
            except ValueError:
                return jsonify({'error': 'Себестоимость должна быть числом'}), 400

            new_product = {
                "sku": str(payload["sku"]).strip(),
                "model": payload.get("model", ""),
                "price": payload.get("price", ""),
                "stock": "",
                "link": "",
                "image": "",
                "category": "",
                "brand": "",
                "first_price": "",
                "position": "",
                "step": "",
                "min": "",
                "max": "",
                "selected": False,
                "auto_down": False,
                "auto_up": False,
                "cost_price": cost_price,
                "removed": False
            }
            last_products.append(new_product)

        except Exception as e:
            return jsonify({'error': f'Ошибка создания: {str(e)}'}), 400

    else:
        found = False
        for product in last_products:
            psku = str(product.get('sku')).strip()
            if psku == sku:
                if key == "cost_price":
                    try:
                        clean_value = str(value).strip().replace(" ", "").replace(",", ".")
                        value = float(clean_value)
                    except ValueError:
                        return jsonify({'error': 'Себестоимость должна быть числом'}), 400

                product[key] = value
                found = True
                print(f"[OK] Обновлено: {sku} → {key} = {value}")
                break

        if not found:
            print(f"[ERROR] Товар с SKU '{sku}' не найден в last_products.json")
            return jsonify({'error': 'Product not found'}), 404

    # === СОХРАНЕНИЕ ===
    try:
        # очищаем от временных ключей
        to_save = []
        for p in last_products:
            p_copy = dict(p)
            p_copy.pop('selected', None)
            to_save.append(p_copy)

        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(to_save, f, ensure_ascii=False, indent=2)


    except Exception as e:
        print(f"[ERROR] Ошибка при сохранении: {e}")
        return jsonify({'error': 'Ошибка при сохранении'}), 500

    return jsonify({"status": "ok"})




@app.route('/api/selected_products')
def api_selected_products():
    email = session.get('kaspi_email')
    if not email:
        return jsonify({})

    path = os.path.join('userdata', email, 'result.csv')
    result = {}

    if os.path.exists(path):
        try:
            with open(path, encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    result[row['SKU']] = {
                        'model': row['Model'],
                        'price': row['Price'],
                        'step': row['Step'],
                        'min': row['Min'],
                        'max': row['Max'],
                        'auto_down': row.get('auto_down', 'true').lower() == 'true',
                        'auto_up': row.get('auto_up', 'true').lower() == 'true'
                    }

        except Exception as e:
            print(f"Ошибка чтения result.csv: {e}")

    return jsonify(result)



@app.route('/api/products')
def api_products():
    global last_products
    return jsonify(last_products)

from datetime import datetime, timedelta

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        code = str(random.randint(100000, 999999))

        # Генерация тестового периода (1 минута, позже days=1)
        trial_period = timedelta(days=1)
        trial_until = (datetime.now() + trial_period).strftime("%Y-%m-%d %H:%M:%S")

        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        try:
            c.execute('''
                INSERT INTO users (email, password, verification_code, trial_until)
                VALUES (?, ?, ?, ?)
            ''', (email, password, code, trial_until))
            conn.commit()

            send_verification_code(email, code)
            session['pending_email'] = email
            flash('Код отправлен на почту', 'success')
            return redirect(url_for('verify'))
        except sqlite3.IntegrityError:
            flash('Пользователь уже существует', 'error')
        finally:
            conn.close()
    return render_template('register.html')


@app.route('/verify', methods=['GET', 'POST'])
def verify():
    email = session.get('pending_email')
    if not email:
        return redirect(url_for('register'))

    if request.method == 'POST':
        code = request.form['code']
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        c.execute('SELECT verification_code FROM users WHERE email = ?', (email,))
        row = c.fetchone()
        if row and row[0] == code:
            c.execute('UPDATE users SET is_verified = 1 WHERE email = ?', (email,))
            conn.commit()
            flash('Подтверждено! Теперь можно войти.', 'success')
            return redirect(url_for('login_user'))
        else:
            flash('Неверный код', 'error')
        conn.close()
    return render_template('verify.html')


import json

def load_smtp_config():
    with open('config.json', 'r', encoding='utf-8') as f:
        return json.load(f)


def send_verification_code(to_email, code):
    config = load_smtp_config()
    msg = MIMEText(f"Ваш код подтверждения: {code}")
    msg['Subject'] = 'Код подтверждения'
    msg['From'] = config['smtp_from']
    msg['To'] = to_email

    with smtplib.SMTP(config['smtp_host'], config['smtp_port']) as server:
        server.starttls()
        server.login(config['smtp_user'], config['smtp_password'])
        server.send_message(msg)
@app.route('/tarify')
def tarify():
    return render_template('tarify.html')


@app.route('/handle_contact', methods=['POST'])
def handle_contact():
    contact = request.form.get('contact')
    
    # Можно логировать или сохранять, пример:
    print(f"[CONTACT] Новый контакт: {contact}")

    # Можно сохранить в файл:
    with open("contacts.txt", "a", encoding="utf-8") as f:
        f.write(contact + "\n")

    flash("Спасибо! Мы свяжемся с вами скоро.", "success")
    return redirect(url_for('landing'))


from datetime import datetime, timedelta
def get_order_entries(entry_id):
    path = os.path.join("userdata", session['register_email'], session['kaspi_email'], "store.json")
    with open(path, encoding="utf-8") as f:
        token = json.load(f).get("api_token")

    headers = {
        "Content-Type": "application/vnd.api+json",
        "X-Auth-Token": token,
        "User-Agent": "Mozilla/5.0"
    }

    url = f"https://kaspi.kz/shop/api/v2/orderentries/{entry_id}"
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return None

        data = resp.json().get("data", {})
        attr = data.get("attributes", {})
        master_id = data.get("relationships", {}).get("masterProduct", {}).get("data", {}).get("id")

        # доп. информация о продукте
        product_info = {}
        try:
            product_resp = requests.get(f"{url}/product", headers=headers, timeout=10)
            if product_resp.status_code == 200:
                prod_data = product_resp.json().get("data", {}).get("attributes", {})
                product_info = {
                    "kaspi_code": prod_data.get("code"),
                    "kaspi_name": prod_data.get("name"),
                    "kaspi_manufacturer": prod_data.get("manufacturer"),
                    "kaspi_category": prod_data.get("category")
                }
        except:
            pass

        return {
            "code": attr.get("code") or product_info.get("kaspi_code"),
            "name": attr.get("name") or product_info.get("kaspi_name"),
            "manufacturer": attr.get("manufacturer") or product_info.get("kaspi_manufacturer"),
            "category": attr.get("category") or product_info.get("kaspi_category"),
            "master_id": master_id,
            "quantity": attr.get("quantity"),
            "price": attr.get("basePrice")
        }
    except:
        return None


def get_merchant_product(master_id):
    if not master_id:
        return {}

    path = os.path.join("userdata", session['register_email'], session['kaspi_email'], "store.json")
    with open(path, encoding="utf-8") as f:
        token = json.load(f).get("api_token")

    headers = {
        "Content-Type": "application/vnd.api+json",
        "X-Auth-Token": token,
        "User-Agent": "Mozilla/5.0"
    }

    url = f"https://kaspi.kz/shop/api/v2/masterproducts/{master_id}/merchantProduct"
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json().get("data", {})
            attr = data.get("attributes", {})
            return {
                "id": data.get("id"),
                "merchant_code": attr.get("code"),
                "merchant_name": attr.get("name"),
                "merchant_manufacturer": attr.get("manufacturer")
            }
    except:
        pass

    return {}



def map_status_kaspi_to_group(status_code: str) -> str:
    if status_code == "KASPI_DELIVERY_CARGO_ASSEMBLY":
        return "PICKING"  # Упаковка

    elif status_code == "KASPI_DELIVERY_WAIT_FOR_COURIER":
        return "READY"  # Передача

    elif status_code == "KASPI_DELIVERY_TRANSMITTED":
        return "DELIVERY"  # Передан на доставку

    elif status_code == "KASPI_DELIVERY_RETURN_REQUEST":
        return "CANCELLED"  # Отменён при доставке

    elif status_code in ["APPROVED_BY_BANK", "NEW"]:
        return "PREORDER"  # Предзаказ

    return "OTHER"
@app.route('/orders')
def orders_page():
    from collections import Counter
    from datetime import datetime, timedelta

    selected_state = request.args.get("state", "ALL")

    if 'register_email' not in session or 'kaspi_email' not in session:
        flash("Сначала подключите магазин", "error")
        return redirect(url_for('settings'))

    register_email = session['register_email']
    kaspi_email = session['kaspi_email']

    path = os.path.join("userdata", register_email, kaspi_email, "store.json")
    with open(path, encoding="utf-8") as f:
        store_data = json.load(f)

    token = store_data.get("api_token")
    if not token:
        flash("API токен не найден", "error")
        return redirect(url_for('settings'))

    headers = {
        "Content-Type": "application/vnd.api+json",
        "X-Auth-Token": token,
        "User-Agent": "Mozilla/5.0"
    }

    # Обработка даты
    date_from_str = request.args.get("date_from")
    date_to_str = request.args.get("date_to")

    try:
        start_date = int(datetime.strptime(date_from_str, "%Y-%m-%d").timestamp() * 1000) if date_from_str \
            else int((datetime.now() - timedelta(days=3)).timestamp() * 1000)
    except:
        start_date = int((datetime.now() - timedelta(days=3)).timestamp() * 1000)

    try:
        end_date = int(datetime.strptime(date_to_str, "%Y-%m-%d").replace(hour=23, minute=59, second=59).timestamp() * 1000) \
            if date_to_str else int(datetime.now().timestamp() * 1000)
    except:
        end_date = int(datetime.now().timestamp() * 1000)

    # Ограничим период до 14 дней максимум (требование Kaspi API)
    max_span = 14 * 24 * 60 * 60 * 1000
    if end_date - start_date > max_span:
        start_date = end_date - max_span

    params = {
        "page[number]": 0,
        "page[size]": 100,
        "filter[orders][creationDate][$ge]": start_date,
        "filter[orders][creationDate][$le]": end_date,
        "include[orders]": "user"
    }

    # Добавим статус для архивных заказов
    if selected_state == "ARCHIVE":
        params["filter[orders][status]"] = "COMPLETED"

    try:
        response = requests.get("https://kaspi.kz/shop/api/v2/orders", headers=headers, params=params, timeout=15)
        all_data = response.json().get("data", []) if response.status_code == 200 else []
    except:
        all_data = []

    now_ms = int(datetime.now().timestamp() * 1000)

    def detect_group(order):
        attr = order["attributes"]
        delivery = attr.get("kaspiDelivery", {})
        courier_date = delivery.get("courierTransmissionPlanningDate")

        if attr.get("status") == "COMPLETED":
            return "ARCHIVE"
        if attr.get("status") in {"CANCELLED", "CANCELLING", "RETURNED", "KASPI_DELIVERY_RETURN_REQUEST"}:
            return "CANCELLED"
        if attr.get("state") == "NEW":
            return "PREORDER"
        if not courier_date:
            return "PICKING"
        if courier_date > now_ms:
            return "READY"
        return "DELIVERY"

    # Фильтрация по статусу (если не ALL)
    filtered_data = all_data if selected_state == "ALL" else [o for o in all_data if detect_group(o) == selected_state]
    status_counts = Counter(detect_group(o) for o in all_data)

    # Подготовка для шаблона
    orders = []
    for order in filtered_data:
        attr = order["attributes"]
        delivery = attr.get("kaspiDelivery", {})
        orders.append({
            "code": attr.get("code"),
            "delivery_type": "Kaspi Доставка" if attr.get("isKaspiDelivery") else attr.get("deliveryMode", "—"),
            "courier_date": delivery.get("courierTransmissionPlanningDate"),
            "delivery_date": delivery.get("plannedDeliveryDate"),
            "delivery_date_text": delivery.get("plannedDeliveryDateText", ""),
            "status": attr.get("status", ""),
            "state": attr.get("state", ""),
            "waybill": attr.get("waybill", ""),
            "full": order
        })

    return render_template("orders.html",
        orders=orders,
        selected_state=selected_state,
        status_counts=status_counts,
        date_from=datetime.fromtimestamp(start_date / 1000).strftime("%Y-%m-%d"),
        date_to=datetime.fromtimestamp(end_date / 1000).strftime("%Y-%m-%d")
    )
@app.route('/dashboard', methods=['GET'])
def dashboard():
    from collections import defaultdict, Counter
    from datetime import datetime, timedelta
    import os, json, requests, re

    def normalize_text(text):
        return re.sub(r'[^a-zA-Z0-9а-яА-Я]', '', text.lower().strip())

    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    city_filter = request.args.get('city', '')
    product_name_filter = request.args.get('product_name', '').strip()

    if not any([start_date, end_date, city_filter, product_name_filter]):
        return render_template("dashboard.html",
            daily_profit=[], daily_gross=[],
            daily_sales=0, daily_sales_data=[],
            daily_quantity=0, daily_quantity_data=[],
            top_cities=[], top_cities_by_orders=[],
            last_products=[], detailed_orders=[],
            top_products=[], all_cities=[], start_date='', end_date=''
        )

    if 'register_email' not in session or 'kaspi_email' not in session:
        flash("Сначала подключите магазин", "error")
        return redirect(url_for('settings'))

    register_email = session['register_email']
    kaspi_email = session['kaspi_email']

    last_products_path = os.path.join("userdata", register_email, kaspi_email, "last_products.json")
    if not os.path.exists(last_products_path):
        flash("Файл last_products.json не найден", "error")
        return redirect(url_for("index"))

    with open(last_products_path, encoding='utf-8') as f:
        last_products = json.load(f)

    store_path = os.path.join("userdata", register_email, kaspi_email, "store.json")
    with open(store_path, encoding="utf-8") as f:
        token = json.load(f).get("api_token")

    if not token:
        flash("API токен не найден", "error")
        return redirect(url_for('settings'))

    headers = {
        "Content-Type": "application/vnd.api+json",
        "X-Auth-Token": token,
        "User-Agent": "Mozilla/5.0"
    }

    try:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    except:
        flash("Некорректный формат даты", "error")
        return redirect(url_for("dashboard"))

    if (end_dt - start_dt).days > 90:
        flash("Максимальный период — 90 дней", "error")
        return redirect(url_for("dashboard"))

    orders = []
    step = timedelta(days=14)
    current_start = start_dt

    while current_start <= end_dt:
        current_end = min(current_start + step - timedelta(days=1), end_dt)

        params = {
            "page[number]": 0,
            "page[size]": 100,
            "filter[orders][status]": "COMPLETED",
            "filter[orders][creationDate][$ge]": int(current_start.timestamp() * 1000),
            "filter[orders][creationDate][$le]": int(current_end.timestamp() * 1000),
            "include[orders]": "user"
        }

        try:
            response = requests.get("https://kaspi.kz/shop/api/v2/orders", headers=headers, params=params, timeout=20)
            part = response.json().get("data", []) if response.status_code == 200 else []
            orders.extend(part)
            print(f"[DEBUG] С {current_start.date()} по {current_end.date()} — загружено заказов: {len(part)}")
        except Exception as e:
            print(f"[ERROR] Ошибка при загрузке заказов с {current_start} по {current_end}: {e}")

        current_start += step

    profit_per_day = defaultdict(float)
    gross_per_day = defaultdict(float)
    sales_by_day = defaultdict(float)
    quantity_by_day = defaultdict(int)
    city_stats = defaultdict(float)
    city_order_counts = Counter()
    product_counter = Counter()
    detailed_orders = []

    for order in orders:
        attr = order.get("attributes", {})
        order_id = order.get("id")
        order_date = datetime.fromtimestamp(attr["creationDate"] / 1000).strftime("%Y-%m-%d")

        delivery_data = attr.get("deliveryAddress", {})
        city = delivery_data.get("town", "-")

        if city_filter and city != city_filter:
            continue

        try:
            resp = requests.get(f"https://kaspi.kz/shop/api/v2/orders/{order_id}/entries", headers=headers, timeout=10)
            entries = resp.json().get("data", []) if resp.status_code == 200 else []
        except:
            entries = []

        order_products = []
        has_missing_cost = False

        for entry in entries:
            e_attr = entry.get("attributes", {})
            name = e_attr.get("name", "")
            qty = int(e_attr.get("quantity", 1))
            price = float(e_attr.get("basePrice", 0))
            entry_id = entry.get("id")

            true_name = ""
            try:
                product_resp = requests.get(
                    f"https://kaspi.kz/shop/api/v2/orderentries/{entry_id}/product",
                    headers=headers,
                    timeout=10
                )
                if product_resp.status_code == 200:
                    prod_data = product_resp.json().get("data", {}).get("attributes", {})
                    true_name = prod_data.get("name", "")
            except:
                pass

            true_name = true_name or name
            product_info = true_name.strip() if true_name else "(название не получено)"

            if product_name_filter and normalize_text(product_name_filter) not in normalize_text(true_name):
                continue

            product = next(
                (p for p in last_products if normalize_text(p.get('model', '')) in normalize_text(true_name)
                 or normalize_text(true_name) in normalize_text(p.get('model', ''))),
                None
            )
            if not product:
                continue

            cost = float(product.get('cost_price', 0) or 0)
            if cost == 0:
                product_info += " (без себестоимости)"
                has_missing_cost = True

            total = price * qty
            profit = total - cost - total * 0.12
            gross = total - cost

            profit_per_day[order_date] += profit
            gross_per_day[order_date] += gross
            sales_by_day[order_date] += total
            quantity_by_day[order_date] += qty
            city_stats[city] += profit
            city_order_counts[city] += 1
            product_counter[product_info] += qty
            order_products.append(product_info)

        if order_products:
            detailed_orders.append({
                "code": attr.get("code"),
                "date": order_date,
                "products": order_products,
                "missing_cost": has_missing_cost
            })

    top_cities = sorted(city_stats.items(), key=lambda x: x[1], reverse=True)
    top_cities_by_orders = city_order_counts.most_common()

    # Сохраняем уникальные города
    cities_path = os.path.join("userdata", register_email, kaspi_email, "cities.json")
    unique_cities = sorted({city for city, _ in city_order_counts.items() if city and city != '-'})
    try:
        with open(cities_path, "w", encoding="utf-8") as f:
            json.dump(unique_cities, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[ERROR] Не удалось сохранить города: {e}")

    # Загружаем сохранённые города
    try:
        with open(cities_path, encoding='utf-8') as f:
            all_cities = json.load(f)
    except:
        all_cities = []

    top_products_raw = product_counter.most_common(5)
    top_products = []
    for name, count in top_products_raw:
        matching = next((p for p in last_products if normalize_text(p.get("model", "")) in normalize_text(name)
                         or normalize_text(name) in normalize_text(p.get("model", ""))), None)
        image = matching.get("image") if matching else ""
        top_products.append({"name": name, "count": count, "image": image})

    return render_template("dashboard.html",
        daily_profit=sorted(profit_per_day.items()),
        daily_gross=sorted(gross_per_day.items()),
        daily_sales=sum(sales_by_day.values()),
        daily_sales_data=sorted(sales_by_day.items()),
        daily_quantity=sum(quantity_by_day.values()),
        daily_quantity_data=sorted(quantity_by_day.items()),
        top_cities=top_cities,
        top_cities_by_orders=top_cities_by_orders,
        last_products=last_products,
        detailed_orders=detailed_orders,
        top_products=top_products,
        all_cities=all_cities,
        start_date=start_date,
        end_date=end_date
    )


@app.route('/orders/archived')
def archived_orders():
    import os, json, requests
    from datetime import datetime, timedelta

    if 'register_email' not in session or 'kaspi_email' not in session:
        return "Магазин не подключён", 403

    register_email = session['register_email']
    kaspi_email = session['kaspi_email']

    # Загружаем API токен
    store_path = os.path.join("userdata", register_email, kaspi_email, "store.json")
    with open(store_path, encoding='utf-8') as f:
        token = json.load(f).get("api_token")

    headers = {
        "Content-Type": "application/vnd.api+json",
        "X-Auth-Token": token,
        "User-Agent": "Mozilla/5.0"
    }

    # Kaspi разрешает максимум 14 дней назад
    date_from = int((datetime.now() - timedelta(days=14)).timestamp() * 1000)

    url = "https://kaspi.kz/shop/api/v2/orders"
    params = {
        "page[number]": 0,
        "page[size]": 100,
        "filter[orders][status]": "COMPLETED",
        "filter[orders][creationDate][$ge]": date_from,
        "include[orders]": "user"
    }

    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        orders = response.json().get("data", []) if response.status_code == 200 else []
    except Exception as e:
        return f"Ошибка загрузки заказов: {e}"

    all_orders = []

    for order in orders:
        attr = order.get("attributes", {})
        order_id = order.get("id")
        code = attr.get("code")
        creation_date = datetime.fromtimestamp(attr.get("creationDate", 0) / 1000).strftime("%Y-%m-%d")
        city = attr.get("cityName", "-")

        # Загружаем товары в заказе
        try:
            resp = requests.get(f"https://kaspi.kz/shop/api/v2/orders/{order_id}/entries", headers=headers, timeout=10)
            entries = resp.json().get("data", []) if resp.status_code == 200 else []
        except Exception as e:
            entries = []

        product_list = [e.get("attributes", {}).get("name", "") for e in entries]

        all_orders.append({
            "code": code,
            "date": creation_date,
            "city": city,
            "products": product_list
        })

    # HTML-вывод
    html = "<h2>🗂 Архивные заказы (только COMPLETED)</h2><ul style='font-family: sans-serif;'>"
    for o in all_orders:
        html += f"<li><b>Код:</b> {o['code']} | <b>Дата:</b> {o['date']} | <b>Город:</b> {o['city']}<br>🛒 {', '.join(o['products'])}</li><hr>"
    html += "</ul>"

    return html

# 📁 payroll_routes.py — вставь в app.py
from flask import render_template, request, redirect, url_for, session, flash
import sqlite3

@app.route('/payroll')
def payroll_home():
    if 'register_email' not in session:
        return redirect(url_for('login_user'))

    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute("SELECT id, month, total FROM payroll_months WHERE register_email = ? ORDER BY id DESC", (session['register_email'],))
    rows = c.fetchall()
    conn.close()

    payrolls = [
        {"id": row[0], "month": row[1], "total": row[2], "month_name": row[1]} for row in rows
    ]
    return render_template("payroll.html", payrolls=payrolls)

@app.route('/payroll/<month>', methods=['GET', 'POST'])
def payroll_month(month):
    if 'register_email' not in session:
        return redirect(url_for('login_user'))

    conn = sqlite3.connect('users.db')
    c = conn.cursor()

    # Проверка — существует ли месяц
    c.execute("SELECT id FROM payroll_months WHERE month = ? AND register_email = ?", (month, session['register_email']))
    row = c.fetchone()

    if row:
        month_id = row[0]
    else:
        # Создание месяца
        c.execute("INSERT INTO payroll_months (register_email, month) VALUES (?, ?)", (session['register_email'], month))
        month_id = c.lastrowid
        conn.commit()

        # Копирование сотрудников из последнего месяца
        c.execute("""
            SELECT id FROM payroll_months
            WHERE register_email = ? AND month != ?
            ORDER BY id DESC LIMIT 1
        """, (session['register_email'], month))
        last = c.fetchone()

        if last:
            last_id = last[0]
            c.execute("""
                SELECT fio, payment_type, oklad FROM payroll_entries
                WHERE month_id = ?
            """, (last_id,))
            for fio, payment_type, oklad in c.fetchall():
                c.execute("""
                    INSERT INTO payroll_entries (month_id, fio, payment_type, oklad, days, bonus, vacation, sick, total)
                    VALUES (?, ?, ?, ?, 0, 0, 0, 0, 0)
                """, (month_id, fio, payment_type, oklad))
        conn.commit()

    # Сохранение данных (POST)
    if request.method == 'POST':
        c.execute("DELETE FROM payroll_entries WHERE month_id = ?", (month_id,))
        total_all = 0
        index = 1

        while True:
            fio = request.form.get(f'fio_{index}')
            if not fio:
                break

            payment_type = request.form.get(f'type_{index}', '')
            oklad = int(request.form.get(f'oklad_{index}', '0') or 0)
            days = int(request.form.get(f'days_{index}', '0') or 0)
            bonus = int(request.form.get(f'bonus_{index}', '0') or 0)
            vacation = int(request.form.get(f'vacation_{index}', '0') or 0)
            sick = int(request.form.get(f'sick_{index}', '0') or 0)

            total = round(oklad * (days / 30) + bonus + vacation + sick)
            total_all += total

            c.execute('''
                INSERT INTO payroll_entries
                (month_id, fio, payment_type, oklad, days, bonus, vacation, sick, total)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (month_id, fio, payment_type, oklad, days, bonus, vacation, sick, total))

            index += 1

        c.execute("UPDATE payroll_months SET total = ? WHERE id = ?", (total_all, month_id))
        conn.commit()
        conn.close()
        flash("Сохранено", "success")
        return redirect(url_for("payroll_month", month=month))

    # Загрузка данных (GET)
    c.execute("SELECT fio, payment_type, oklad, days, bonus, vacation, sick, total FROM payroll_entries WHERE month_id = ?", (month_id,))
    rows = c.fetchall()
    conn.close()

    data = [
        {
            "fio": r[0],
            "type": r[1],
            "oklad": r[2],
            "days": r[3],
            "bonus": r[4],
            "vacation": r[5],
            "sick": r[6],
            "total": r[7]
        } for r in rows
    ]
    total_all = sum(r['total'] for r in data)

    return render_template("payroll_month.html", rows=data, total_all=total_all, month=month, month_name=month)




def download_waybill(order_code, kaspi_email, kaspi_password):
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    import time, os

    download_dir = os.path.abspath("downloads")
    os.makedirs(download_dir, exist_ok=True)

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-blink-features=AutomationControlled")
    prefs = {
        "download.default_directory": download_dir,
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "plugins.always_open_pdf_externally": True,
    }
    options.add_experimental_option("prefs", prefs)

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    wait = WebDriverWait(driver, 20)

    try:
        driver.get("https://idmc.shop.kaspi.kz/login")
        wait.until(EC.presence_of_element_located((By.ID, "user_email_field"))).send_keys(kaspi_email)
        wait.until(EC.element_to_be_clickable((By.CLASS_NAME, "button"))).click()
        wait.until(EC.presence_of_element_located((By.ID, "password_field"))).send_keys(kaspi_password)
        wait.until(EC.element_to_be_clickable((By.CLASS_NAME, "button"))).click()
        wait.until(EC.url_contains("kaspi.kz/mc"))
        time.sleep(3)

        driver.get(f"https://kaspi.kz/mc/#/orders/{order_code}?from=KASPI_DELIVERY_WAIT_FOR_COURIER")
        time.sleep(1)

        existing_files = set(os.listdir(download_dir))

        # Клик по кнопке "Скачать накладную"
        waybill_btn = driver.find_element(By.XPATH, "//a[contains(@href,'kaspi-delivery/waybill')]")
        waybill_btn.click()

        # Ждём загрузку PDF
        for _ in range(20):
            time.sleep(0.5)
            new_files = set(os.listdir(download_dir)) - existing_files
            pdfs = [f for f in new_files if f.lower().endswith(".pdf")]
            if pdfs:
                return os.path.join(download_dir, pdfs[0])
    except Exception as e:
        print(f"[!] Ошибка при скачивании накладной: {e}")
    finally:
        driver.quit()

    return None


from flask import send_file

@app.route("/download-waybill/<order_code>")
def download_waybill_route(order_code):
    kaspi_email = session.get("kaspi_email")
    kaspi_password = session.get("kaspi_password")

    if not kaspi_email or not kaspi_password:
        flash("Сначала войдите и подключите магазин", "error")
        return redirect(url_for("login_user"))

    path = download_waybill(order_code, kaspi_email, kaspi_password)
    if path and os.path.exists(path):
        return send_file(path, as_attachment=True)
    else:
        flash("❌ Не удалось скачать накладную", "error")
        return redirect(url_for("orders_page", state="READY"))

def detect_group(order):
    attr = order["attributes"]
    delivery = attr.get("kaspiDelivery", {})
    courier_date = delivery.get("courierTransmissionPlanningDate")
    now_ms = int(datetime.now().timestamp() * 1000)

    status = attr.get("status")
    state = attr.get("state")

    if status == "COMPLETED":
        return "ARCHIVE"
    if status in {"CANCELLED", "CANCELLING", "RETURNED", "KASPI_DELIVERY_RETURN_REQUEST"}:
        return "CANCELLED"
    if state == "NEW":
        return "PREORDER"
    if not courier_date:
        return "PICKING"
    if courier_date > now_ms:
        return "READY"
    return "DELIVERY"



@app.route("/order_entries/<order_id>")
def get_order_items(order_id):
    if 'register_email' not in session or 'kaspi_email' not in session:
        return jsonify([])

    path = os.path.join("userdata", session['register_email'], session['kaspi_email'], "store.json")
    with open(path, encoding="utf-8") as f:
        token = json.load(f).get("api_token")

    headers = {
        "Content-Type": "application/vnd.api+json",
        "X-Auth-Token": token,
        "User-Agent": "Mozilla/5.0"
    }

    url = f"https://kaspi.kz/shop/api/v2/orders/{order_id}/entries"

    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            return jsonify([])

        data = response.json().get("data", [])
        result = []

        for entry in data:
            attr = entry.get("attributes", {})
            rel = entry.get("relationships", {})
            master_id = rel.get("masterProduct", {}).get("data", {}).get("id")
            entry_id = entry.get("id")

            # Дополнительно получаем полную информацию о товаре
            name = attr.get("name")
            code = attr.get("code")
            manufacturer = attr.get("manufacturer")
            category = attr.get("category")

            try:
                product_resp = requests.get(f"https://kaspi.kz/shop/api/v2/orderentries/{entry_id}/product", headers=headers, timeout=5)
                if product_resp.status_code == 200:
                    product_data = product_resp.json().get("data", {}).get("attributes", {})
                    name = name or product_data.get("name")
                    code = code or product_data.get("code")
                    manufacturer = manufacturer or product_data.get("manufacturer")
                    category = category or product_data.get("category")
            except Exception as e:
                print(f"[!] Ошибка при получении продукта: {e}")

            item = {
                "code": code,
                "name": name,
                "quantity": attr.get("quantity"),
                "price": attr.get("basePrice"),
                "category": category,
                "master_id": master_id
            }

            result.append(item)

        return jsonify(result)

    except Exception as e:
        print(f"Ошибка при получении товаров заказа: {e}")
        return jsonify([])



def human_status(state, status):
    mapping = {
        ("KASPI_DELIVERY_CARGO_ASSEMBLY", "ACCEPTED_BY_MERCHANT"): "🧃 Упаковка",
        ("KASPI_DELIVERY_WAIT_FOR_COURIER", "ASSEMBLE"): "📤 Передача",
        ("KASPI_DELIVERY_TRANSMITTED", "ACCEPTED_BY_MERCHANT"): "🚚 На доставке",
        ("KASPI_DELIVERY_TRANSMITTED", "COMPLETED"): "✅ Доставлен",
        ("KASPI_DELIVERY_RETURN_REQUEST", "CANCELLED"): "❌ Отменён при доставке",
        ("KASPI_DELIVERY_RETURN_REQUEST", "ARRIVED_BACKWARD"): "📦 Возврат доставлен",
        (None, "CANCELLED"): "❌ Отменён",
        (None, "ARRIVED"): "🏬 Прибыл на склад",
        (None, "APPROVED_BY_BANK"): "⏳ Ожидает принятия магазином",
    }
    return mapping.get((state, status)) or mapping.get((None, status)) or f"{state or ''} / {status or ''}"


@app.route("/orders_filters")
def orders_filters():
    selected_state = request.args.get("state", "ALL")

    filters = {
        "ALL": "Все",
        "PICKING": "Упаковка",
        "READY_FOR_DELIVERY": "Передача",
        "KASPI_DELIVERY": "Переданы на доставку",
        "CANCELLED": "Отменены"
    }

    # ⚠️ Загружаем сохранённые filter_counts из сессии или базы
    filter_counts = session.get("filter_counts", {k: "..." for k in filters})

    return render_template("partials/orders_filters.html", filters=filters, filter_counts=filter_counts, selected_state=selected_state)



@app.template_filter('datetimeformat')
def datetimeformat(value):
    try:
        return datetime.fromtimestamp(int(value) / 1000).strftime('%d.%m.%Y %H:%M')
    except:
        return ''


import base64
from werkzeug.utils import secure_filename

import base64
from werkzeug.utils import secure_filename
from flask import send_from_directory

@app.route('/api/add_product', methods=['POST'])
def api_add_product():
    try:
        payload = request.get_json()
        if not payload:
            return jsonify({'error': 'Пустой запрос'}), 400

        # ✅ Обработка base64 изображения
        image_data = payload.get("images", [{}])[0].get("url", "")
        if image_data.startswith("data:image/"):
            try:
                header, encoded = image_data.split(",", 1)
                ext = header.split('/')[1].split(';')[0]
                image_bytes = base64.b64decode(encoded)

                filename = secure_filename(f"{payload['sku']}.{ext}")
                upload_path = get_user_upload_folder()
                os.makedirs(upload_path, exist_ok=True)
                file_path = os.path.join(upload_path, filename)

                with open(file_path, "wb") as f:
                    f.write(image_bytes)

                payload["images"] = [{"url": f"/uploads/{filename}"}]
            except Exception as e:
                return jsonify({'error': f'Ошибка обработки изображения: {e}'}), 500

        # ✅ Получаем токен
        store_path = os.path.join("userdata", session['register_email'], session['kaspi_email'], "store.json")
        with open(store_path, encoding="utf-8") as f:
            token = json.load(f).get("api_token")

        if not token:
            return jsonify({'error': 'API токен не найден'}), 403

        # ✅ Отправка запроса в Kaspi
        headers = {
            "Content-Type": "text/plain",
            "Accept": "application/json",
            "X-Auth-Token": token
        }

        response = requests.post("https://kaspi.kz/shop/api/products/import",
                                 headers=headers,
                                 data=json.dumps([payload]))

        if response.status_code == 200:
            return jsonify({'status': 'ok'})
        else:
            return jsonify({'error': f"Kaspi ответил: {response.text}"}), 500

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


from flask import send_from_directory

@app.route('/uploads/<filename>')
def serve_uploaded_image(filename):
    upload_folder = get_user_upload_folder()
    return send_from_directory(upload_folder, filename)


@app.route('/api/kaspi/categories')
def get_kaspi_categories():
    try:
        path = os.path.join("userdata", session['register_email'], session['kaspi_email'], "store.json")
        with open(path, encoding="utf-8") as f:
            token = json.load(f).get("api_token")

        headers = {
            "Accept": "application/json",
            "X-Auth-Token": token
        }

        res = requests.get("https://kaspi.kz/shop/api/products/classification/categories", headers=headers)
        return jsonify(res.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/kaspi/attributes')
def get_kaspi_attributes():
    try:
        category = request.args.get("c")
        if not category:
            return jsonify({"error": "category code (c) required"}), 400

        path = os.path.join("userdata", session['register_email'], session['kaspi_email'], "store.json")
        with open(path, encoding="utf-8") as f:
            token = json.load(f).get("api_token")

        headers = {
            "Accept": "application/json",
            "X-Auth-Token": token
        }

        url = f"https://kaspi.kz/shop/api/products/classification/attributes?c={category}"
        res = requests.get(url, headers=headers)
        return jsonify(res.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@app.route('/nomenclature')
def nomenclature_page():
    if 'register_email' not in session or 'kaspi_email' not in session:
        flash("Сначала подключите магазин", "error")
        return redirect(url_for('settings'))

    folder = get_user_folder()
    products = []

    try:
        with open(os.path.join(folder, 'last_products.json'), encoding='utf-8') as f:
            products = json.load(f)
    except:
        flash("Ошибка загрузки номенклатуры", "error")

    return render_template('nomenclature.html', products=products)

# Подписка
from datetime import datetime

def is_user_paid(register_email=None):
    if not register_email:
        register_email = session.get('register_email')
        if not register_email:
            return False

    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('SELECT paid_until, trial_until FROM users WHERE email = ?', (register_email,))
    row = c.fetchone()
    conn.close()

    if not row:
        return False

    paid_until, trial_until = row
    now = datetime.now()

    if paid_until:
        try:
            return now < datetime.strptime(paid_until, "%Y-%m-%d")
        except:
            return False

    if trial_until:
        try:
            return now < datetime.strptime(trial_until, "%Y-%m-%d %H:%M:%S")
        except:
            return False

    return False

@app.route('/')
def root():
    if 'register_email' in session:
        return redirect(url_for('index'))
    return redirect(url_for('landing'))  # перенаправление на отдельную страницу

@app.route('/landing')
def landing():
    return render_template('landing.html')

    
def count_user_skus():
    folder = get_user_folder()
    if not folder:
        return 0  # пользователь не авторизован или магазин не подключен

    path = os.path.join(folder, 'last_products.json')
    if not os.path.exists(path):
        return 0

    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        return len([p for p in data if not p.get('removed')])
    except:
        return 0
def get_subscription_price(sku_count):
    email = session.get('register_email')
    conn = sqlite3.connect('users.db')
    c = conn.cursor()

    c.execute('''
        SELECT plans.sku_limit, plans.price
        FROM users
        JOIN plans ON users.plan_id = plans.id
        WHERE users.email = ?
    ''', (email,))
    
    result = c.fetchone()
    conn.close()

    if result:
        plan_limit, plan_price = result
        if sku_count > plan_limit:
            return -1  # превышен лимит
        return plan_price  # нормальный тариф
    return -1


if __name__ == '__main__':
    init_db()
    app.run(debug=True)

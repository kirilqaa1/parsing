# email_utils.py
import smtplib
from email.mime.text import MIMEText

def send_verification_code(to_email, code):
    msg = MIMEText(f"Ваш код подтверждения: {code}")
    msg['Subject'] = 'Код подтверждения'
    msg['From'] = 'noreply@example.com'
    msg['To'] = to_email

    with smtplib.SMTP('smtp.yourmailserver.com', 587) as server:
        server.starttls()
        server.login('your_login', 'your_password')
        server.send_message(msg)

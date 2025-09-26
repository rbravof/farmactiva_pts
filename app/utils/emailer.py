# app/utils/emailer.py
import os, smtplib, ssl, sys
from email.message import EmailMessage

def _tf(v: str | None) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")

def send_email(to: str, subject: str, html: str, text: str | None = None) -> bool:
    host = os.getenv("SMTP_HOST", "")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER", "")
    pwd  = os.getenv("SMTP_PASS", "")
    from_addr = os.getenv("SMTP_FROM", user or "no-reply@example.com")

    use_ssl   = _tf(os.getenv("SMTP_SSL")) or port == 465
    skip_tls  = _tf(os.getenv("SMTP_SKIP_TLS"))  # útil p/ servers old o si ya vas con SSL:465
    debug_lvl = 1 if _tf(os.getenv("SMTP_DEBUG")) else 0

    if not (host and port and user and pwd and from_addr and to):
        print(f"[EMAIL] Config incompleta "
              f"host={host!r} port={port} user={'***' if user else ''} from={from_addr!r} to={to!r}")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"]    = from_addr
    msg["To"]      = to
    if not text:
        text = "Este mensaje contiene contenido HTML."
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    try:
        if use_ssl:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as s:
                s.set_debuglevel(debug_lvl)
                s.login(user, pwd)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.set_debuglevel(debug_lvl)
                s.ehlo()
                if not skip_tls:
                    context = ssl.create_default_context()
                    s.starttls(context=context)
                    s.ehlo()
                s.login(user, pwd)
                s.send_message(msg)

        print(f"[EMAIL] Enviado a {to} asunto={subject!r} via {host}:{port} "
              f"ssl={use_ssl} starttls={not use_ssl and not skip_tls}")
        return True

    except smtplib.SMTPAuthenticationError as e:
        print(f"[EMAIL] Auth ERROR {e.smtp_code} {e.smtp_error}")
    except smtplib.SMTPResponseException as e:
        print(f"[EMAIL] SMTP ERROR {e.smtp_code} {e.smtp_error}")
    except Exception as e:
        print(f"[EMAIL] Error al enviar a {to}: {e}")
    return False

if __name__ == "__main__":
    # CLI: python -m app.emailer test@example.com "Asunto (opcional)"
    to = sys.argv[1] if len(sys.argv) > 1 else os.getenv("TEST_TO", "")
    subj = sys.argv[2] if len(sys.argv) > 2 else "Prueba SMTP"
    html = "<h1>Prueba SMTP</h1><p>Si ves esto, el correo salió.</p>"
    ok = send_email(to, subj, html, "Prueba SMTP")
    print("OK" if ok else "FAIL")

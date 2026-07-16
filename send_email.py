#!/usr/bin/env python3.11
"""复用发信脚本：读取 /workspace/email_config.json，向默认收件人发邮件（支持附件）。

用法:
  python3.11 send_email.py --subject "标题" --body "正文" [--attach 文件1 文件2 ...]
  python3.11 send_email.py --subject "标题" --body-file body.txt [--attach a.py b.py]
"""
import os, sys, ssl, argparse, json
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

CFG = "/workspace/email_config.json"


def load_cfg():
    with open(CFG, encoding="utf-8") as f:
        return json.load(f)


def send(subject, body, attaches=None):
    c = load_cfg()
    attaches = attaches or []
    msg = MIMEMultipart()
    msg["From"], msg["To"], msg["Subject"] = c["email"], c["default_recipient"], subject
    msg.attach(MIMEText(body, "plain", "utf-8"))
    for p in attaches:
        if not os.path.exists(p):
            print(f"跳过缺失附件: {p}")
            continue
        with open(p, "rb") as fh:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(fh.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f"attachment; filename={os.path.basename(p)}")
        msg.attach(part)
        print(f"已附加: {os.path.basename(p)}")
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(c["smtp_host"], c["smtp_port"], context=ctx) as s:
        s.login(c["email"], c["auth_code"])
        s.send_message(msg)
    print(f"已发送至 {c['default_recipient']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--subject", required=True)
    ap.add_argument("--body", default=None)
    ap.add_argument("--body-file", default=None)
    ap.add_argument("--attach", nargs="*", default=[])
    a = ap.parse_args()
    body = a.body
    if a.body_file:
        with open(a.body_file, encoding="utf-8") as f:
            body = f.read()
    if not body:
        body = sys.stdin.read()
    send(a.subject, body, a.attach)

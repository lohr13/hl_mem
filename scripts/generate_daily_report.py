"""生成 provider 日报并可选通过 SMTP 发送。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from hl_mem.monitoring.channels import EmailChannel
from hl_mem.monitoring.reports import generate_daily_report
from hl_mem.settings import Settings
from hl_mem.storage.database import Database


def main() -> None:
    """从配置数据库生成日报；配置完整时发送邮件。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--send-email", action="store_true")
    args = parser.parse_args()
    settings = Settings.from_env()
    database = Database(settings.database_path)
    connection = database.open()
    report = generate_daily_report(connection)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered)
    if args.send_email:
        if not all((settings.smtp_host, settings.alert_email_from, settings.alert_email_to)):
            raise RuntimeError(
                "email report requires HL_MEM_SMTP_HOST, HL_MEM_ALERT_EMAIL_FROM and HL_MEM_ALERT_EMAIL_TO"
            )
        EmailChannel(
            settings.smtp_host,
            settings.smtp_port,
            settings.alert_email_from,
            settings.alert_email_to,
        ).send("HL-Mem daily provider report", report)
    database.close()


if __name__ == "__main__":
    main()

# services/email_service.py
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os
from typing import Optional
import aiosmtplib



class EmailService:
    async def send_share_notification(
        self,
        to_email: str,
        shared_by_email: str,
        file_name: str,
        permission: str,
        note: Optional[str] = None,
        shared_by_name: Optional[str] = None
    ):
        subject = f"File shared with you: {file_name}"

        text_body = f"""
        {shared_by_name or shared_by_email} shared a file with you.

        File: {file_name}
        Permission: {permission.upper()}
        """

        note_section = ""
        if note:
            note_section = f"""
            <div style="background-color:#fff7ed; border-left:4px solid #f59e0b; padding:12px; margin-top:16px; border-radius:4px;">
                <p style="margin:0; font-size:13px; color:#92400e;">
                    <strong>Note from sender:</strong><br>
                    {note}
                </p>
            </div>
            """

        html_body = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <title>File Shared</title>
        </head>
        <body style="margin:0; padding:0; background-color:#f3f4f6;">
            <div style="max-width:600px; margin:0 auto; padding:24px;">
                <div style="background-color:#ffffff; border-radius:8px; padding:24px; font-family:Arial, sans-serif;">

                    <h2 style="margin-top:0; color:#1d4ed8; font-size:20px;">
                        A file has been shared with you
                    </h2>

                    <p style="font-size:14px; color:#111827; margin-bottom:16px;">
                        <strong>{shared_by_name or shared_by_email}</strong> has shared a file with you.
                    </p>

                    <table width="100%" cellpadding="0" cellspacing="0"
                        style="background-color:#f9fafb; border-radius:6px; padding:16px;">
                        <tr>
                            <td style="font-size:14px; color:#374151; padding-bottom:8px;">
                                <strong>File name</strong>
                            </td>
                            <td style="font-size:14px; color:#111827; padding-bottom:8px;">
                                {file_name}
                            </td>
                        </tr>
                        <tr>
                            <td style="font-size:14px; color:#374151;">
                                <strong>Access level</strong>
                            </td>
                            <td style="font-size:14px; color:#111827;">
                                {permission.upper()}
                            </td>
                        </tr>
                    </table>

                    {note_section}

                    <p style="font-size:12px; color:#6b7280; margin-top:24px;">
                        This is an automated message. Please sign in to the application to view or manage the shared file.
                    </p>

                </div>
            </div>
        </body>
        </html>
        """

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        from_name = os.getenv("SMTP_FROM_NAME")
        from_email = os.getenv("SMTP_FROM_EMAIL")
        msg["From"] = f"{from_name} <{from_email}>"
        msg["To"] = to_email

        msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        smtp = aiosmtplib.SMTP(
            hostname= os.getenv("SMTP_HOST"),
            port= int(os.getenv("SMTP_PORT")),
            timeout=10
        )

        await smtp.connect()
        await smtp.login(
            os.getenv("SMTP_USER"),   
            os.getenv("SMTP_PASS")
        )
        await smtp.send_message(msg)
        await smtp.quit()

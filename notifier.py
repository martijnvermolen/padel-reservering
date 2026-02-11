"""
notifier.py - E-mail notificatie module voor reserveringsresultaten.

Verstuurt e-mail bij succes of falen van een reservering.
"""

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


class EmailNotifier:
    """Verstuurt e-mail notificaties over reserveringsresultaten."""

    def __init__(self, config: dict):
        """
        Args:
            config: Het email-gedeelte van de configuratie.
        """
        self.enabled = config.get("enabled", False)
        self.smtp_server = config.get("smtp_server", "smtp.gmail.com")
        self.smtp_port = config.get("smtp_port", 587)
        self.use_tls = config.get("use_tls", True)
        self.afzender = config.get("afzender", "")
        self.ontvanger = config.get("ontvanger", "")

        # Wachtwoord uit config of environment variable
        self.wachtwoord = config.get("afzender_wachtwoord", "")
        if not self.wachtwoord:
            self.wachtwoord = os.environ.get("EMAIL_PASSWORD", "")

    def verstuur(self, result: dict) -> bool:
        """
        Verstuur een e-mail notificatie met het reserveringsresultaat.

        Args:
            result: Dict met reserveringsresultaat (success, datum, tijd, baan, spelers, foutmelding).

        Returns:
            True als de e-mail succesvol is verstuurd.
        """
        if not self.enabled:
            logger.info("E-mail notificaties zijn uitgeschakeld")
            return False

        if not self.afzender or not self.ontvanger or not self.wachtwoord:
            logger.warning(
                "E-mail configuratie incompleet. "
                "Controleer afzender, ontvanger en wachtwoord (EMAIL_PASSWORD env variable)."
            )
            return False

        try:
            subject = self._maak_onderwerp(result)
            body_html = self._maak_body_html(result)
            body_text = self._maak_body_text(result)

            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = self.afzender
            msg["To"] = self.ontvanger

            msg.attach(MIMEText(body_text, "plain", "utf-8"))
            msg.attach(MIMEText(body_html, "html", "utf-8"))

            logger.info(f"E-mail versturen naar {self.ontvanger}...")

            if self.use_tls:
                with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                    server.ehlo()
                    server.starttls()
                    server.ehlo()
                    server.login(self.afzender, self.wachtwoord)
                    server.sendmail(self.afzender, self.ontvanger, msg.as_string())
            else:
                with smtplib.SMTP_SSL(self.smtp_server, self.smtp_port) as server:
                    server.login(self.afzender, self.wachtwoord)
                    server.sendmail(self.afzender, self.ontvanger, msg.as_string())

            logger.info("E-mail succesvol verstuurd!")
            return True

        except smtplib.SMTPAuthenticationError:
            logger.error(
                "E-mail authenticatie mislukt. Controleer je e-mail wachtwoord. "
                "Voor Gmail: gebruik een App Password (https://myaccount.google.com/apppasswords)"
            )
            return False
        except Exception as e:
            logger.error(f"Fout bij versturen e-mail: {e}")
            return False

    def _maak_onderwerp(self, result: dict) -> str:
        """Maak het e-mail onderwerp."""
        if result.get("success"):
            return f"Padel gereserveerd: {result.get('datum', '?')} om {result.get('tijd', '?')}"
        else:
            return f"Padel reservering MISLUKT: {result.get('datum', '?')}"

    def _maak_body_text(self, result: dict) -> str:
        """Maak de platte tekst body van de e-mail."""
        lines = []

        if result.get("success"):
            lines.append("Je padelbaan is succesvol gereserveerd!")
            lines.append("")
        else:
            lines.append("De padelbaan reservering is MISLUKT.")
            lines.append("")

        lines.append(f"Datum:    {result.get('datum', 'onbekend')}")
        lines.append(f"Tijd:     {result.get('tijd', 'onbekend')}")
        lines.append(f"Baan:     {result.get('baan', 'onbekend')}")

        spelers = result.get("spelers", [])
        if spelers:
            lines.append(f"Spelers:  {', '.join(spelers)}")

        if result.get("foutmelding"):
            lines.append("")
            lines.append(f"Melding:  {result['foutmelding']}")

        lines.append("")
        lines.append("---")
        lines.append("Automatisch verstuurd door Padel Reservering Bot")

        return "\n".join(lines)

    def _maak_body_html(self, result: dict) -> str:
        """Maak de HTML body van de e-mail."""
        success = result.get("success", False)
        status_color = "#28a745" if success else "#dc3545"
        status_text = "GERESERVEERD" if success else "MISLUKT"
        status_icon = "&#9989;" if success else "&#10060;"

        spelers = result.get("spelers", [])
        spelers_html = ", ".join(spelers) if spelers else "<em>geen</em>"

        foutmelding_html = ""
        if result.get("foutmelding"):
            foutmelding_html = f"""
            <tr>
                <td style="padding: 8px; font-weight: bold; color: #555;">Melding</td>
                <td style="padding: 8px; color: #dc3545;">{result['foutmelding']}</td>
            </tr>"""

        return f"""
        <html>
        <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
            <div style="background-color: {status_color}; color: white; padding: 20px; text-align: center; border-radius: 8px 8px 0 0;">
                <h1 style="margin: 0; font-size: 24px;">{status_icon} Padel Reservering {status_text}</h1>
            </div>
            <div style="border: 1px solid #ddd; border-top: none; padding: 20px; border-radius: 0 0 8px 8px;">
                <table style="width: 100%; border-collapse: collapse;">
                    <tr>
                        <td style="padding: 8px; font-weight: bold; color: #555; width: 100px;">Datum</td>
                        <td style="padding: 8px;">{result.get('datum', 'onbekend')}</td>
                    </tr>
                    <tr style="background-color: #f8f9fa;">
                        <td style="padding: 8px; font-weight: bold; color: #555;">Tijd</td>
                        <td style="padding: 8px;">{result.get('tijd', 'onbekend')}</td>
                    </tr>
                    <tr>
                        <td style="padding: 8px; font-weight: bold; color: #555;">Baan</td>
                        <td style="padding: 8px;">{result.get('baan', 'onbekend')}</td>
                    </tr>
                    <tr style="background-color: #f8f9fa;">
                        <td style="padding: 8px; font-weight: bold; color: #555;">Spelers</td>
                        <td style="padding: 8px;">{spelers_html}</td>
                    </tr>
                    {foutmelding_html}
                </table>
            </div>
            <p style="color: #999; font-size: 12px; text-align: center; margin-top: 16px;">
                Automatisch verstuurd door Padel Reservering Bot - TPV Heksenwiel
            </p>
        </body>
        </html>
        """

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
            # Voeg korte foutreden toe aan onderwerp
            fout = result.get("foutmelding", "")
            korte_reden = self._korte_foutreden(fout)
            if korte_reden:
                return f"Padel MISLUKT ({korte_reden}): {result.get('datum', '?')}"
            return f"Padel reservering MISLUKT: {result.get('datum', '?')}"

    def _korte_foutreden(self, foutmelding: str) -> str:
        """Maak een korte samenvatting van de foutmelding voor het e-mail onderwerp."""
        if not foutmelding:
            return ""
        fout_lower = foutmelding.lower()
        if "padelbanen zijn bezet" in fout_lower or "alle padelbanen" in fout_lower:
            return "geen padelbaan vrij"
        if "heeft al een reservering" in fout_lower:
            return "speler heeft al reservering"
        if "maximaal" in fout_lower:
            return "max reserveringen bereikt"
        if "niet toegestaan" in fout_lower:
            return "niet toegestaan"
        if "login" in fout_lower:
            return "login mislukt"
        if "timeout" in fout_lower:
            return "timeout"
        if "geen beschikbaar" in fout_lower or "geen padelbaan" in fout_lower:
            return "geen baan beschikbaar"
        return ""

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
        lines.append(f"Tijd:     {result.get('tijd') or 'niet gereserveerd'}")
        lines.append(f"Baan:     {result.get('baan') or 'niet gereserveerd'}")

        spelers = result.get("spelers", [])
        if spelers:
            lines.append(f"Spelers:  {', '.join(spelers)}")

        if result.get("foutmelding") and not result.get("success"):
            lines.append("")
            lines.append("REDEN:")
            lines.append(f"  {result['foutmelding']}")

            # Voeg suggestie toe
            suggestie = self._maak_suggestie(result["foutmelding"])
            if suggestie:
                lines.append("")
                lines.append("WAT KUN JE DOEN?")
                lines.append(f"  {suggestie}")

        lines.append("")
        lines.append("---")
        lines.append("Automatisch verstuurd door Padel Reservering Bot")

        return "\n".join(lines)

    def _maak_suggestie(self, foutmelding: str) -> str:
        """Geef een suggestie op basis van de foutmelding."""
        fout_lower = foutmelding.lower()

        if "padelbanen zijn bezet" in fout_lower or "alle padelbanen" in fout_lower:
            return ("Alle padelbanen waren al gereserveerd op het gewenste tijdstip. "
                    "Probeer handmatig te reserveren voor een ander tijdstip via "
                    "https://tpv-heksenwiel.knltb.site/me/ReservationsPlayers")

        if "heeft al een reservering" in fout_lower:
            return ("Een van de geselecteerde spelers heeft al een reservering binnen "
                    "2 uur van het gewenste tijdstip. Pas de spelerslijst aan in het "
                    "dashboard of kies een ander tijdstip.")

        if "maximaal" in fout_lower:
            return ("Het maximaal aantal reserveringen is bereikt. "
                    "Annuleer eerst een bestaande reservering als je een nieuwe wilt plaatsen.")

        if "login" in fout_lower:
            return ("Controleer of de inloggegevens (KNLTB_USERNAME / KNLTB_PASSWORD) "
                    "nog correct zijn in de GitHub Secrets.")

        if "gewenste tijden" in fout_lower and "wel beschikbaar" in fout_lower:
            return ("De gewenste tijden waren niet beschikbaar, maar er zijn wel "
                    "andere tijden vrij. Pas eventueel de tijden aan in het dashboard.")

        return ""

    def _maak_body_html(self, result: dict) -> str:
        """Maak de HTML body van de e-mail."""
        success = result.get("success", False)
        status_color = "#28a745" if success else "#dc3545"
        status_text = "GERESERVEERD" if success else "MISLUKT"
        status_icon = "&#9989;" if success else "&#10060;"

        spelers = result.get("spelers", [])
        spelers_html = ", ".join(spelers) if spelers else "<em>geen</em>"

        tijd_html = result.get('tijd') or '<em>niet gereserveerd</em>'
        baan_html = result.get('baan') or '<em>niet gereserveerd</em>'

        # Foutmelding sectie (alleen bij falen)
        fout_sectie_html = ""
        if result.get("foutmelding") and not success:
            foutmelding = result['foutmelding']
            suggestie = self._maak_suggestie(foutmelding)
            suggestie_html = ""
            if suggestie:
                suggestie_html = f"""
                <div style="background-color: #fff3cd; border: 1px solid #ffc107; border-radius: 6px; padding: 12px; margin-top: 12px;">
                    <strong style="color: #856404;">Wat kun je doen?</strong>
                    <p style="color: #856404; margin: 6px 0 0 0; font-size: 14px;">{suggestie}</p>
                </div>"""

            fout_sectie_html = f"""
                <div style="background-color: #f8d7da; border: 1px solid #f5c6cb; border-radius: 6px; padding: 12px; margin-top: 16px;">
                    <strong style="color: #721c24;">Reden:</strong>
                    <p style="color: #721c24; margin: 6px 0 0 0; font-size: 14px;">{foutmelding}</p>
                </div>
                {suggestie_html}"""

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
                        <td style="padding: 8px;">{tijd_html}</td>
                    </tr>
                    <tr>
                        <td style="padding: 8px; font-weight: bold; color: #555;">Baan</td>
                        <td style="padding: 8px;">{baan_html}</td>
                    </tr>
                    <tr style="background-color: #f8f9fa;">
                        <td style="padding: 8px; font-weight: bold; color: #555;">Spelers</td>
                        <td style="padding: 8px;">{spelers_html}</td>
                    </tr>
                </table>
                {fout_sectie_html}
            </div>
            <p style="color: #999; font-size: 12px; text-align: center; margin-top: 16px;">
                Automatisch verstuurd door Padel Reservering Bot - TPV Heksenwiel
            </p>
        </body>
        </html>
        """

"""
api_bot.py - Directe HTTP-gebaseerde reservering voor KNLTB baanreservering.

Vervangt de Playwright browser-automatisering door directe HTTP-calls.
Dezelfde interface als ReserveringBot zodat main.py het als drop-in kan gebruiken.

Flow:
  1. POST /mijn                          -> Login (session cookies)
  2. POST /Ajax/Profile/AddPlayer?id=... -> Spelers toevoegen (per speler)
  3. POST /me/ReservationsPlayersPost    -> Spelers bevestigen
  4. POST /me/ReservationsDay            -> Dag + dagdeel selecteren
  5. POST /me/ReservationsCourt          -> Baan + tijd selecteren
  6. POST /Ajax/Profile/SaveReservation  -> Reservering bevestigen (AJAX)
"""

import html as html_mod
import logging
import os
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests

NL_TZ = ZoneInfo("Europe/Amsterdam")

BASE_URL = "https://tpv-heksenwiel.knltb.site"
HTTP_TIMEOUT = 15  # Seconden per HTTP-request (voorkomt dat trage responses de retry-loop blokkeren)

PADEL_COURTS = {
    1: "27247a4e-0443-411a-be10-ba08ccd40cde",
    2: "3920ba34-a11e-4108-ae84-b51d469659ac",
    3: "4ce24896-1ca0-48cd-8c53-2d7ccf354b84",
    4: "47acc23a-8c78-4316-8f70-b052c95ea910",
}

PADEL_COURT_NAMES = {v: f"Padel {k}" for k, v in PADEL_COURTS.items()}


class ReserveringError(Exception):
    pass


class _TimeoutSession(requests.Session):
    """Session met een standaard timeout op alle requests."""

    def request(self, *args, **kwargs):
        kwargs.setdefault("timeout", HTTP_TIMEOUT)
        return super().request(*args, **kwargs)


class ApiReserveringBot:
    """Directe HTTP-gebaseerde padelbaan reservering via KNLTB.site."""

    def __init__(self, config: dict, label: str = ""):
        self.config = config
        self.label = label
        self._log = logging.getLogger(f"{__name__}.{label}" if label else __name__)
        self._session: requests.Session | None = None
        self._speler_guids: dict[str, str] = {}
        self._laatste_fout: str | None = None

    def start(self):
        """Start een HTTP-sessie en log in."""
        self._session = _TimeoutSession()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        })
        self._login()

    def stop(self):
        """Sluit de HTTP-sessie."""
        if self._session:
            self._session.close()
            self._session = None
        self._log.info("Sessie gesloten")

    def _login(self):
        creds = self.config["credentials"]
        username = os.environ.get("KNLTB_USERNAME", "") or creds.get("username", "")
        password = os.environ.get("KNLTB_PASSWORD", "") or creds.get("password", "")
        if not username or not password:
            raise ReserveringError("Geen credentials geconfigureerd")

        self._log.info(f"Inloggen als {username}...")
        resp = self._session.post(f"{BASE_URL}/mijn", data={
            "Login.LoginType": "FedmembershipNumber",
            "Login.MembershipNumber": username,
            "Login.Password": password,
        }, allow_redirects=True)

        page_lower = resp.text.lower()

        if resp.status_code != 200:
            raise ReserveringError(f"Login mislukt (HTTP {resp.status_code})")

        # Detecteer of het login-formulier nog zichtbaar is (= login mislukt)
        login_form_indicators = ['type="password"', "type='password'", 'login.password']
        if any(indicator in page_lower for indicator in login_form_indicators):
            self._log.error(f"Login-formulier nog zichtbaar na POST - credentials incorrect?")
            self._log.debug(f"Response URL: {resp.url}")
            raise ReserveringError("Login mislukt - login-formulier nog zichtbaar (controleer credentials)")

        if len(self._session.cookies) == 0:
            raise ReserveringError("Login mislukt - geen sessie-cookies ontvangen")

        self._log.info(f"Login geslaagd ({len(self._session.cookies)} cookies, URL: {resp.url})")

    def _get_csrf(self, html: str) -> str:
        match = re.search(r'__RequestVerificationToken.*?value="([^"]+)"', html)
        if not match:
            raise ReserveringError("CSRF token niet gevonden")
        return match.group(1)

    def _parse_speler_cards(self, html: str) -> dict[str, str]:
        """
        Parse HTML met addPlayer cards om naam->GUID mapping te extraheren.

        De cards hebben dit formaat:
          <div class="card-body addPlayer" ... data-id="GUID">
              <img ...>
              Naam
              <a ...>...</a>
          </div>
        """
        guids = {}
        for m in re.finditer(
            r'class="card-body\s+addPlayer"[^>]*data-id="([a-f0-9-]+)"[^>]*>(.*?)</div>',
            html,
            re.DOTALL,
        ):
            guid = m.group(1)
            inner = re.sub(r'<[^>]+>', ' ', m.group(2))
            inner = html_mod.unescape(re.sub(r'\s+', ' ', inner).strip())
            if inner:
                guids[inner] = guid

        return guids

    def _ontdek_speler_guids(self) -> dict[str, str]:
        """Parse de ReservationsPlayers pagina om recente speler GUIDs te vinden."""
        resp = self._session.get(f"{BASE_URL}/me/ReservationsPlayers")
        if resp.status_code != 200:
            raise ReserveringError(f"Kon spelers-pagina niet laden (status {resp.status_code})")

        guids = self._parse_speler_cards(resp.text)

        self._log.info(f"Recente speler GUIDs gevonden: {len(guids)}")
        for naam, guid in guids.items():
            self._log.debug(f"  {naam} = {guid}")

        return guids

    def _zoek_spelers(self, zoekterm: str) -> dict[str, str]:
        """Zoek spelers via de AJAX search-endpoint met een zoekterm."""
        resp = self._session.get(
            f"{BASE_URL}/Ajax/Profile/SearchPlayers",
            params={"term": zoekterm},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        if resp.status_code != 200:
            self._log.warning(f"Spelers zoeken mislukt (status {resp.status_code})")
            return {}

        return self._parse_speler_cards(resp.text)

    def _zoek_alle_spelers(self) -> dict[str, str]:
        """
        Haal alle clubleden op door per letter a-z te zoeken.
        De API retourneert max 20 resultaten per zoekterm; door elke letter
        apart te zoeken bereiken we goede dekking van het ledenbestand.
        """
        import string

        alle_spelers: dict[str, str] = {}
        for letter in string.ascii_lowercase:
            resultaten = self._zoek_spelers(letter)
            nieuwe = {k: v for k, v in resultaten.items() if k not in alle_spelers}
            alle_spelers.update(resultaten)

            if resultaten and nieuwe:
                self._log.debug(
                    f"  Zoek '{letter}': {len(resultaten)} gevonden, "
                    f"{len(nieuwe)} nieuw (totaal: {len(alle_spelers)})"
                )

        self._log.info(f"Alle spelers via search API: {len(alle_spelers)}")
        return alle_spelers

    def _voeg_spelers_toe(self, spelers: list[str]) -> int:
        """Voeg spelers toe via AJAX en return het aantal succesvol toegevoegde."""
        if not self._speler_guids:
            self._speler_guids = self._ontdek_speler_guids()

        toegevoegd = 0
        for speler in spelers:
            guid = self._speler_guids.get(speler)
            if not guid:
                # Zoek op achternaam in bekende GUIDs
                achternaam = speler.split()[-1].lower()
                for naam, g in self._speler_guids.items():
                    if achternaam in naam.lower():
                        guid = g
                        break

            if not guid:
                # Speler niet in recente lijst, zoek via search API
                self._log.info(f"Speler '{speler}' niet in recente lijst, zoek via API...")
                zoek_resultaten = self._zoek_spelers(speler)
                if zoek_resultaten:
                    guid = next(iter(zoek_resultaten.values()))
                    naam = next(iter(zoek_resultaten.keys()))
                    self._speler_guids[naam] = guid
                    self._log.info(f"Gevonden via search: {naam} ({guid[:8]}...)")

            if not guid:
                self._log.warning(f"Geen GUID gevonden voor '{speler}'")
                continue

            resp = self._session.post(
                f"{BASE_URL}/Ajax/Profile/AddPlayer?id={guid}",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            if resp.status_code == 200:
                toegevoegd += 1
                self._log.info(f"Speler toegevoegd: {speler} ({guid[:8]}...)")
            else:
                self._log.warning(f"Speler '{speler}' toevoegen mislukt: {resp.status_code}")

        return toegevoegd

    def _submit_spelers(self, csrf: str):
        """Submit de spelerslijst (stap 1 -> stap 2)."""
        resp = self._session.post(
            f"{BASE_URL}/me/ReservationsPlayersPost",
            data={"__RequestVerificationToken": csrf},
            allow_redirects=True,
        )
        self._log.debug(f"ReservationsPlayersPost -> status {resp.status_code}, URL: {resp.url}")
        if resp.status_code != 200:
            raise ReserveringError(f"Spelers submiten mislukt (status {resp.status_code})")
        self._log.info(f"Spelers ingediend, naar dag-selectie (URL: {resp.url})")
        return resp

    def _selecteer_dag(self, target_date: date, tijden: list[str]) -> str:
        """Selecteer dag + dagdeel en return de HTML van de baan-pagina."""
        eerste_tijd = tijden[0] if tijden else "19:00"
        uur = int(eerste_tijd.split(":")[0])
        if uur < 12:
            dagdeel_uur = 8
        elif uur < 17:
            dagdeel_uur = 12
        else:
            dagdeel_uur = 18

        # Construeer TZ-aware datetime via constructor (niet replace()) voor DST-safety
        if isinstance(target_date, date) and not isinstance(target_date, datetime):
            dagdeel_dt = datetime(
                target_date.year, target_date.month, target_date.day,
                dagdeel_uur, 0, 0, tzinfo=NL_TZ,
            )
        else:
            dagdeel_dt = target_date.replace(
                hour=dagdeel_uur, minute=0, second=0, microsecond=0, tzinfo=NL_TZ,
            )
        utc_offset = dagdeel_dt.strftime("%z")
        tz_formatted = utc_offset[:3] + ":" + utc_offset[3:]  # "+0100" -> "+01:00"
        dagdeel_suffix = f"T{dagdeel_uur:02d}:00:00{tz_formatted}"

        selected_date = target_date.strftime("%Y-%m-%d") + dagdeel_suffix

        # Haal CSRF token van de ReservationsDay pagina
        day_page = self._session.get(f"{BASE_URL}/me/ReservationsDay")
        self._log.debug(f"ReservationsDay GET -> status {day_page.status_code}, URL: {day_page.url}")
        csrf = self._get_csrf(day_page.text)

        self._log.info(f"Selecteer dag: {selected_date}")
        resp = self._session.post(
            f"{BASE_URL}/me/ReservationsDay",
            data={
                "__RequestVerificationToken": csrf,
                "selectedDate": selected_date,
            },
            allow_redirects=True,
        )

        self._log.debug(f"ReservationsDay POST -> status {resp.status_code}, URL: {resp.url}")

        if resp.status_code != 200 or "ReservationsCourt" not in resp.url:
            self._log.error(f"Dag selectie response body (500 chars): {resp.text[:500]}")
            raise ReserveringError(
                f"Dag selectie mislukt (status {resp.status_code}, url={resp.url})"
            )

        self._log.info(f"Dag geselecteerd, op baan-pagina (URL: {resp.url})")

        # Dump court HTML voor diagnose
        try:
            from pathlib import Path
            dump_path = Path(__file__).parent / "court_dump.html"
            with open(dump_path, "w", encoding="utf-8") as f:
                f.write(resp.text)
            self._log.debug(f"Court HTML gedumpt naar: {dump_path}")
        except Exception as e:
            self._log.debug(f"Court HTML dump mislukt: {e}")

        return resp.text

    def _parse_beschikbare_slots(self, court_html: str) -> list[dict]:
        """Parse de baan-pagina voor beschikbare tijdslots.

        HTML structuur (KNLTB site):
            <div class="timeincourt [disabled]" data-hour="N">
                <select data-court="GUID" [disabled]>
                    <option value="START_DT" data-end-time="END_DT" data-price="...">HH:MM</option>
                    ...
                </select>
            </div>

        Elke <option> is een boekbaar tijdslot met start- en eindtijd.
        """
        slots = []
        disabled_count = 0
        empty_count = 0

        for tic_match in re.finditer(
            r'<div\b[^>]*class="([^"]*\btimeincourt\b[^"]*)"[^>]*>(.*?)</div>',
            court_html, re.DOTALL,
        ):
            tic_classes = tic_match.group(1)
            tic_inner = tic_match.group(2)

            if 'disabled' in tic_classes.split():
                disabled_count += 1
                continue

            select_match = re.search(
                r'<select\b([^>]*)data-court="([a-f0-9-]+)"([^>]*)>(.*?)</select>',
                tic_inner, re.DOTALL,
            )
            if not select_match:
                continue

            select_pre = select_match.group(1)
            court_guid = select_match.group(2)
            select_post = select_match.group(3)
            select_inner = select_match.group(4)

            if court_guid not in PADEL_COURT_NAMES:
                continue

            if 'disabled' in (select_pre + select_post):
                disabled_count += 1
                continue

            options_found = False
            for opt_match in re.finditer(r'<option\b([^>]*)>([^<]*)</option>', select_inner):
                opt_attrs = opt_match.group(1)

                value_m = re.search(r'value="([^"]+)"', opt_attrs)
                end_m = re.search(r'data-end-?time="([^"]+)"', opt_attrs)
                if not value_m or not end_m:
                    continue

                start_full = value_m.group(1).strip()
                end_full = end_m.group(1).strip()
                start_short = re.search(r'(\d{2}:\d{2})', start_full)
                end_short = re.search(r'(\d{2}:\d{2})', end_full)

                if start_short and end_short:
                    options_found = True
                    slots.append({
                        "court_guid": court_guid,
                        "court_name": PADEL_COURT_NAMES[court_guid],
                        "start_full": start_full,
                        "end_full": end_full,
                        "start_time": start_short.group(1),
                        "end_time": end_full,
                        "end_time_short": end_short.group(1),
                    })

            if not options_found:
                empty_count += 1

        self._log.info(
            f"Beschikbare padel-slots: {len(slots)} "
            f"(disabled: {disabled_count}, leeg: {empty_count})"
        )

        if len(slots) == 0:
            all_tic_tags = re.findall(r'<[^>]*\btimeincourt\b[^>]*>', court_html)
            self._log.warning(
                f"0 beschikbare slots "
                f"(disabled: {disabled_count}, leeg: {empty_count}, "
                f"totaal timeincourt: {len(all_tic_tags)})"
            )

        for s in slots:
            self._log.debug(f"  {s['court_name']} {s['start_time']}-{s['end_time_short']}")
        return slots

    def _vind_beste_slot(
        self, slots: list[dict], tijden: list[str],
        baan_voorkeur: list[int],
    ) -> dict | None:
        """Vind het beste beschikbare slot op basis van tijd- en baanvoorkeur.

        Elke slot bevat al start_full en end_full uit de <option> tags.
        Match direct op start_time (HH:MM) en voorkeursbaan.
        """
        if not slots:
            return None

        for gewenste_tijd in tijden:
            for baan_nr in (baan_voorkeur or [1, 2, 3, 4]):
                court_guid = PADEL_COURTS.get(baan_nr)
                if not court_guid:
                    continue

                matching = [
                    s for s in slots
                    if s["court_guid"] == court_guid and s["start_time"] == gewenste_tijd
                ]

                if matching:
                    slot = matching[0]
                    self._log.info(
                        f"Beste slot: {slot['start_time']}-{slot['end_time_short']} "
                        f"op {slot['court_name']}"
                    )
                    return slot

            self._log.debug(f"Tijd {gewenste_tijd} niet beschikbaar op voorkeursbanen")

        self._log.warning("Geen geschikt slot gevonden voor gewenste tijden")
        return None

    def _reserveer_baan(self, court_html: str, slot: dict, dry_run: bool = False) -> tuple[bool, str]:
        """Selecteer de baan+tijd en bevestig de reservering."""
        csrf = self._get_csrf(court_html)

        self._log.info(
            f"Reserveer {slot['court_name']} van {slot['start_time']} "
            f"({slot['start_full']} - {slot['end_full']})"
        )

        if dry_run:
            return (True, f"DRY RUN - {slot['court_name']} om {slot['start_time']}")

        # Stap 3: POST court selectie
        resp = self._session.post(
            f"{BASE_URL}/me/ReservationsCourt",
            data={
                "__RequestVerificationToken": csrf,
                "selectedCourt": slot["court_guid"],
                "selectedDate": slot["start_full"],
                "selectedEndDate": slot["end_full"],
            },
            allow_redirects=True,
        )

        self._log.debug(f"ReservationsCourt POST -> status {resp.status_code}, URL: {resp.url}")

        if resp.status_code != 200:
            return (False, f"Court selectie mislukt (status {resp.status_code})")

        # Check of we op de bevestigingspagina zijn
        if "ReservationsConfirm" in resp.url or "Confirm" in resp.url:
            return self._bevestig_reservering(resp.text)

        # Misschien is het direct bevestigd
        page_text = resp.text.lower()
        success_indicators = [
            "reservering geplaatst", "reservering bevestigd",
            "succesvol gereserveerd", "gelukt", "bevestigd",
        ]
        for indicator in success_indicators:
            if indicator in page_text:
                return (True, f"Reservering bevestigd ({indicator})")

        error_patterns = [
            "heeft al een reservering", "maximaal", "bezet",
            "niet beschikbaar", "er is een fout", "fout opgetreden",
            "an error occurred", "server error",
        ]
        for pattern in error_patterns:
            if pattern in page_text:
                return (False, f"Fout: {pattern}")

        # Als we nog op de Court pagina zijn, is er iets misgegaan
        if "ReservationsCourt" in resp.url:
            return (False, "Baan selectie niet geaccepteerd - mogelijk al bezet")

        self._log.warning(f"Onbekende status na court POST. URL: {resp.url}")
        return (False, "Onbekende status - controleer handmatig")

    def _bevestig_reservering(self, confirm_html: str) -> tuple[bool, str]:
        """Stap 4: Bevestig de reservering via AJAX call.

        De KNLTB-site gebruikt geen form POST voor bevestiging maar een AJAX call:
            <a id="confirmReservationButton"
               data-url="/Ajax/Profile/SaveReservation"
               data-redirect="/me/Reservations">
        """
        self._log.info("Bevestigingspagina bereikt, bevestig reservering...")

        # Dump confirm HTML voor diagnose
        try:
            from pathlib import Path
            dump_path = Path(__file__).parent / "confirm_dump.html"
            with open(dump_path, "w", encoding="utf-8") as f:
                f.write(confirm_html)
            self._log.debug(f"Confirm HTML gedumpt naar: {dump_path}")
        except Exception as e:
            self._log.debug(f"Confirm HTML dump mislukt: {e}")

        # Zoek de AJAX save-URL uit de confirm-button
        save_match = re.search(
            r'id="confirmReservationButton"[^>]*data-url="([^"]+)"', confirm_html
        )
        if not save_match:
            save_match = re.search(r'data-url="(/Ajax/Profile/SaveReservation[^"]*)"', confirm_html)

        save_url = save_match.group(1) if save_match else "/Ajax/Profile/SaveReservation"
        if save_url.startswith("/"):
            save_url = f"{BASE_URL}{save_url}"

        self._log.info(f"AJAX bevestiging via: {save_url}")

        resp = self._session.post(
            save_url,
            headers={"X-Requested-With": "XMLHttpRequest"},
            allow_redirects=True,
        )

        self._log.debug(
            f"SaveReservation -> status {resp.status_code}, "
            f"URL: {resp.url}, body: {resp.text[:300]}"
        )

        if resp.status_code == 200:
            body = resp.text.strip().lower()
            error_patterns = [
                "heeft al een reservering", "maximaal", "niet beschikbaar",
                "er is een fout", "fout opgetreden", "server error",
            ]
            for pattern in error_patterns:
                if pattern in body:
                    return (False, f"Bevestiging mislukt: {pattern}")

            self._log.info("SaveReservation OK (status 200)")
            return (True, "Reservering bevestigd via AJAX")

        return (False, f"SaveReservation mislukt (status {resp.status_code})")

    # =========================================================================
    # SPELER DISCOVERY
    # =========================================================================

    def haal_alle_spelers(self) -> dict[str, str]:
        """
        Haal alle beschikbare clubleden op via de search API.
        Vereist een actieve sessie (eerst de spelers-pagina bezoeken).

        Returns:
            Dict van {naam: guid} voor alle beschikbare spelers.
        """
        self._session.get(f"{BASE_URL}/me/ReservationsPlayers")
        return self._zoek_alle_spelers()

    # =========================================================================
    # PUBLIEKE INTERFACE (compatible met main.py)
    # =========================================================================

    def voorbereiden(
        self, target_date: date, tijden: list[str], spelers: list[str],
    ) -> str | None:
        """
        Bereid de reservering voor: login is al gedaan in start().
        Voeg spelers toe en submit de spelerslijst.

        Returns:
            None bij succes, foutmelding bij falen.
        """
        try:
            # Laad spelers-pagina en voeg spelers toe
            players_page = self._session.get(f"{BASE_URL}/me/ReservationsPlayers")
            self._log.debug(
                f"ReservationsPlayers GET -> status {players_page.status_code}, "
                f"URL: {players_page.url}"
            )
            if players_page.status_code != 200:
                return f"Kon spelers-pagina niet laden (status {players_page.status_code})"

            # Detecteer redirect naar login (= sessie verlopen)
            if 'type="password"' in players_page.text.lower():
                return "Sessie verlopen - login-pagina getoond i.p.v. spelers-pagina"

            csrf = self._get_csrf(players_page.text)

            # Ontdek speler GUIDs
            self._speler_guids = self._ontdek_speler_guids()

            toegevoegd = self._voeg_spelers_toe(spelers)
            if toegevoegd == 0:
                return "Geen enkele speler kon worden toegevoegd"

            self._log.info(f"{toegevoegd}/{len(spelers)} spelers toegevoegd")

            # Submit spelers
            self._submit_spelers(csrf)
            return None

        except ReserveringError as e:
            return str(e)
        except (requests.Timeout, requests.ConnectionError) as e:
            return f"Verbindingsfout bij voorbereiding: {e}"
        except Exception as e:
            return f"Onverwachte fout bij voorbereiding: {e}"

    def probeer_reserveer(
        self,
        target_date: date,
        tijden: list[str],
        spelers: list[str],
        baan_voorkeur: list = None,
        dry_run: bool = False,
        is_eerste_poging: bool = False,
    ) -> dict:
        """
        Probeer de reservering uit te voeren.

        Returns:
            Dict met: success, tijd, baan, foutmelding, retry.
        """
        result = {
            "success": False,
            "tijd": None,
            "baan": None,
            "foutmelding": None,
            "retry": False,
        }

        try:
            if not is_eerste_poging:
                # Herstart wizard: spelers opnieuw toevoegen
                fout = self.voorbereiden(target_date, tijden, spelers)
                if fout:
                    result["foutmelding"] = fout
                    result["retry"] = True
                    return result

            # Selecteer dag
            court_html = self._selecteer_dag(target_date, tijden)

            # Parse beschikbare slots
            slots = self._parse_beschikbare_slots(court_html)
            if not slots:
                self._laatste_fout = "Geen beschikbare padel-slots gevonden"
                result["foutmelding"] = self._laatste_fout
                result["retry"] = True
                return result

            # Vind beste slot
            slot = self._vind_beste_slot(slots, tijden, baan_voorkeur or [])
            if not slot:
                beschikbare = sorted(set(s.get("start_time", "?") for s in slots))
                self._laatste_fout = (
                    f"Geen padelbaan vrij op gewenste tijden ({', '.join(tijden)}). "
                    f"Wel slots beschikbaar rond: {', '.join(beschikbare[:5])}"
                )
                result["foutmelding"] = self._laatste_fout
                result["retry"] = True
                return result

            result["tijd"] = slot["start_time"]
            result["baan"] = slot["court_name"]

            # Reserveer
            succes, detail = self._reserveer_baan(court_html, slot, dry_run)
            if succes:
                result["success"] = True
                result["foutmelding"] = detail if dry_run else None
            else:
                result["foutmelding"] = detail
                fout_lower = detail.lower()
                no_retry = ["heeft al een reservering", "maximaal", "niet toegestaan"]
                result["retry"] = not any(ind in fout_lower for ind in no_retry)

        except ReserveringError as e:
            result["foutmelding"] = str(e)
            result["retry"] = True
        except (requests.Timeout, requests.ConnectionError) as e:
            result["foutmelding"] = f"Verbindingsfout: {e}"
            result["retry"] = True
            self._log.warning(f"HTTP timeout/verbindingsfout: {e}")
        except Exception as e:
            result["foutmelding"] = f"Fout: {e}"
            result["retry"] = True
            self._log.error(f"Fout bij reserveerpoging: {e}", exc_info=True)

        return result

    def reserveer(
        self,
        target_date: date,
        tijden: list[str],
        spelers: list[str],
        baan_voorkeur: list = None,
        dry_run: bool = False,
    ) -> dict:
        """Volledige reservering in één keer (voor lokaal testen)."""
        result = {
            "success": False,
            "datum": target_date.strftime("%d-%m-%Y"),
            "tijd": None,
            "baan": None,
            "spelers": spelers,
            "foutmelding": None,
        }

        try:
            fout = self.voorbereiden(target_date, tijden, spelers)
            if fout:
                result["foutmelding"] = fout
                return result

            poging = self.probeer_reserveer(
                target_date, tijden, spelers, baan_voorkeur, dry_run,
                is_eerste_poging=True,
            )
            result["success"] = poging["success"]
            result["tijd"] = poging["tijd"]
            result["baan"] = poging["baan"]
            result["foutmelding"] = poging["foutmelding"]

        except Exception as e:
            result["foutmelding"] = f"Onverwachte fout: {e}"
            self._log.error(f"Onverwachte fout: {e}", exc_info=True)

        return result

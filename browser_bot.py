"""
browser_bot.py - Playwright browser-automatisering voor KNLTB baanreservering.

Volgt de 4-stappen wizard van KNLTB.site:
  1. Partners kiezen
  2. Kies een dag
  3. Kies een baan
  4. Bevestigen
"""

import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from playwright.sync_api import sync_playwright, Page, Browser, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)

SCREENSHOTS_DIR = Path(__file__).parent / "screenshots"


class ReserveringError(Exception):
    """Fout tijdens het reserveringsproces."""
    pass


class ReserveringBot:
    """Automatische padelbaan reservering via KNLTB.site."""

    def __init__(self, config: dict, verbose_screenshots: bool = False, label: str = ""):
        self.config = config
        self.browser: Browser | None = None
        self.page: Page | None = None
        self.playwright = None
        self.verbose_screenshots = verbose_screenshots
        self.label = label  # Optioneel label voor logging (bijv. "Bot-A", "Bot-B")
        self._laatste_fout: str | None = None  # Specifieke foutmelding van laatste actie
        # Maak een logger met het label zodat alle logregels herkenbaar zijn
        self._log = logging.getLogger(f"{__name__}.{label}" if label else __name__)
        self._ensure_screenshots_dir()

    def _ensure_screenshots_dir(self):
        """Maak screenshots directory aan als die niet bestaat."""
        SCREENSHOTS_DIR.mkdir(exist_ok=True)

    def _screenshot(self, name: str, force: bool = False):
        """Maak een screenshot. Alleen bij fouten of force=True, tenzij verbose_screenshots aan staat."""
        if not self.page:
            return
        if not force and not self.verbose_screenshots:
            return
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = name.replace(":", "-").replace(" ", "_")
        label_prefix = f"{self.label}_" if self.label else ""
        path = SCREENSHOTS_DIR / f"{timestamp}_{label_prefix}{safe_name}.png"
        try:
            self.page.screenshot(path=str(path), full_page=True)
            self._log.debug(f"Screenshot opgeslagen: {path}")
        except Exception as e:
            self._log.warning(f"Kon screenshot niet maken: {e}")

    def start(self):
        """Start de browser."""
        browser_config = self.config.get("browser", {})
        headless = browser_config.get("headless", True)
        slow_mo = browser_config.get("slow_mo", 0)
        timeout = browser_config.get("timeout", 30000)

        self._log.info(f"Browser starten (headless={headless}, slow_mo={slow_mo}ms)")

        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(
            headless=headless,
            slow_mo=slow_mo,
        )
        self.page = self.browser.new_page()
        self.page.set_default_timeout(timeout)
        self.page.set_viewport_size({"width": 1280, "height": 900})

    def stop(self):
        """Sluit de browser."""
        if self.browser:
            self.browser.close()
            self.browser = None
        if self.playwright:
            self.playwright.stop()
            self.playwright = None
        self._log.info("Browser gesloten")

    # =========================================================================
    # LOGIN
    # =========================================================================

    def login(self, target_url: str = None) -> bool:
        """
        Log in op het KNLTB reserveringssysteem.

        Als target_url is opgegeven, navigeert de browser direct naar die URL.
        De site redirect automatisch naar het login-formulier als je niet
        ingelogd bent. Na login ben je dan direct op de gewenste pagina.
        Dit bespaart een extra navigatie-stap (~2-3s).

        Args:
            target_url: Optionele URL om direct naartoe te navigeren.
                        Als None, wordt de login-URL uit de config gebruikt.

        Returns:
            True als login succesvol is.
        """
        creds = self.config["credentials"]
        urls = self.config["urls"]
        login_type = creds.get("login_type", "bondsnummer")

        # Lees username uit env var, fallback naar config
        username = os.environ.get("KNLTB_USERNAME", "") or creds.get("username", "")
        if not username:
            raise ReserveringError(
                "Geen gebruikersnaam geconfigureerd. Stel KNLTB_USERNAME in of vul config.yaml in."
            )

        # Lees password uit env var, fallback naar config
        password = os.environ.get("KNLTB_PASSWORD", "") or creds.get("password", "")
        if not password:
            raise ReserveringError(
                "Geen wachtwoord geconfigureerd. Stel KNLTB_PASSWORD in of vul config.yaml in."
            )

        self._log.info(f"Inloggen met {login_type}: {username}")
        nav_url = target_url or urls["login"]
        self._log.info(f"Navigeer naar: {nav_url}")

        try:
            self.page.goto(nav_url, wait_until="domcontentloaded")
            self.page.wait_for_load_state("networkidle", timeout=10000)
            self._screenshot("01_login_pagina")

            # Als we al ingelogd zijn (geen login-formulier zichtbaar), klaar
            if self._is_logged_in():
                self._log.info("Al ingelogd - geen login nodig")
                return True

            # Selecteer login type als nodig
            if login_type == "clublidnummer":
                clubnr_option = self.page.locator("text=Clublidnummer")
                if clubnr_option.is_visible():
                    clubnr_option.click()
                    self.page.wait_for_timeout(300)

            # Vul gebruikersnaam in
            username_field = self.page.locator(
                "input[type='text'], input[type='number'], "
                "input[name*='user'], input[name*='login'], "
                "input[name*='bonds'], input[placeholder*='ummer']"
            ).first
            username_field.fill(str(username))
            self._log.debug("Gebruikersnaam ingevuld")

            # Vul wachtwoord in
            password_field = self.page.locator("input[type='password']").first
            password_field.fill(password)
            self._log.debug("Wachtwoord ingevuld")

            self._screenshot("02_login_ingevuld")

            # Klik op inloggen
            login_button = self.page.locator(
                "button:has-text('Log in'), input[type='submit']:has-text('Log in'), "
                "button:has-text('Inloggen'), a:has-text('Log in')"
            ).first
            login_button.click()

            self.page.wait_for_load_state("networkidle", timeout=10000)
            self._screenshot("03_na_login")

            if self._is_logged_in():
                self._log.info("Login succesvol!")
                return True
            else:
                self._log.error("Login mislukt - nog steeds op login pagina")
                self._screenshot("03_login_mislukt", force=True)
                return False

        except PlaywrightTimeout as e:
            self._log.error(f"Timeout tijdens inloggen: {e}")
            self._screenshot("03_login_timeout", force=True)
            return False
        except Exception as e:
            self._log.error(f"Fout tijdens inloggen: {e}")
            self._screenshot("03_login_fout", force=True)
            raise ReserveringError(f"Login mislukt: {e}")

    def _is_logged_in(self) -> bool:
        """Controleer of we succesvol zijn ingelogd."""
        login_form_visible = self.page.locator("input[type='password']").is_visible()
        return not login_form_visible

    # =========================================================================
    # STAP 0: NAVIGEER NAAR RESERVERINGSPAGINA
    # =========================================================================

    def navigeer_naar_reservering(self) -> bool:
        """Navigeer naar de reserveringspagina (baan reserveren wizard)."""
        urls = self.config["urls"]
        self._log.info("Navigeren naar reserveringspagina...")

        try:
            self.page.goto(urls["reservering"], wait_until="domcontentloaded")
            self.page.wait_for_load_state("networkidle", timeout=10000)
            self._screenshot("04_reservering_pagina")

            # Controleer of we op stap 1 (Partners kiezen) zijn
            stap1 = self.page.locator("text=Partners kiezen")
            if stap1.is_visible():
                self._log.info("Reserveringspagina geladen - Stap 1: Partners kiezen")
                return True

            # Probeer op "Baan reserveringen" tab te klikken
            baan_tab = self.page.locator(
                "a:has-text('Baan reserveringen'), "
                "a:has-text('Baan reserveren')"
            ).first
            if baan_tab.is_visible():
                baan_tab.click()
                self.page.wait_for_load_state("networkidle", timeout=10000)
                self._screenshot("04b_baan_tab_geklikt")

            self._log.info(f"Huidige URL: {self.page.url}")
            return True

        except Exception as e:
            self._log.error(f"Fout bij navigatie: {e}")
            self._screenshot("04_navigatie_fout", force=True)
            return False

    # =========================================================================
    # STAP 1: PARTNERS KIEZEN
    # =========================================================================

    def stap1_partners_kiezen(self, spelers: list[str]) -> bool:
        """
        Stap 1 van de wizard: Voeg medespelers toe.

        De pagina toont "Recent mee gespeeld" met spelers die een "+" knop hebben.
        Er is ook een zoekbalk "Spelers" om op naam te zoeken.

        Args:
            spelers: Lijst met namen van medespelers.

        Returns:
            True als minimaal 1 speler is toegevoegd en we naar stap 2 kunnen.
        """
        if not spelers:
            self._log.warning("Geen medespelers opgegeven")
            return False

        self._log.info(f"Stap 1: Partners kiezen - {spelers}")
        spelers_toegevoegd = 0

        for speler in spelers:
            if self._voeg_speler_toe(speler):
                spelers_toegevoegd += 1

        self._screenshot("05_spelers_toegevoegd")
        self._log.info(f"{spelers_toegevoegd}/{len(spelers)} spelers toegevoegd")

        if spelers_toegevoegd == 0:
            self._log.error("Geen enkele speler toegevoegd")
            return False

        # Klik op "Volgende >" om naar stap 2 te gaan
        return self._klik_volgende("stap1")

    def _voeg_speler_toe(self, speler: str) -> bool:
        """
        Voeg een enkele speler toe.

        Probeert eerst via "Recent mee gespeeld" (klik op "+" naast de naam),
        daarna via het zoekveld.
        """
        self._log.info(f"Speler toevoegen: {speler}")

        # Methode 1: Zoek in "Recent mee gespeeld" lijst
        # Elke speler is een element met de naam en een "+" knop ernaast
        # We zoeken op (deel van) de naam
        achternaam = speler.split()[-1] if " " in speler else speler

        # Zoek een container/kaart die de spelernaam bevat en een "+" knop heeft
        # De structuur is: een element met de naam + een "+" icoon/knop ernaast
        recent_spelers = self.page.locator(
            f"text='{speler}'"
        )

        if recent_spelers.count() > 0:
            # Gevonden in "Recent mee gespeeld" - klik op het element of de "+" ernaast
            speler_element = recent_spelers.first

            # Zoek de "+" knop in de buurt van dit element
            # De "+" knop is waarschijnlijk een sibling of in dezelfde parent container
            parent = speler_element.locator("xpath=..")
            plus_button = parent.locator("svg, .add, [class*='add'], img[alt='+']")

            if plus_button.count() > 0 and plus_button.first.is_visible():
                plus_button.first.click()
                self.page.wait_for_timeout(300)
                self._log.info(f"Speler '{speler}' toegevoegd via Recent mee gespeeld (+)")
                return True

            # Probeer het hele parent element te klikken (de kaart zelf)
            try:
                parent.click()
                self.page.wait_for_timeout(300)
                self._log.info(f"Speler '{speler}' toegevoegd via klik op kaart")
                return True
            except Exception:
                pass

        # Methode 2: Zoek op achternaam als volledige naam niet werkt
        if achternaam != speler:
            recent_achternaam = self.page.locator(f"text='{achternaam}'")
            if recent_achternaam.count() > 0:
                parent = recent_achternaam.first.locator("xpath=..")
                try:
                    parent.click()
                    self.page.wait_for_timeout(300)
                    self._log.info(f"Speler '{speler}' toegevoegd via achternaam '{achternaam}'")
                    return True
                except Exception:
                    pass

        # Methode 3: Gebruik het zoekveld "Spelers"
        self._log.info(f"Speler '{speler}' niet in Recent - probeer zoekbalk")
        search_field = self.page.locator(
            "input[placeholder*='peler'], input[placeholder*='oek'], "
            "input[type='search'], input[name*='search']"
        ).first

        if not search_field.is_visible():
            # Probeer het zoekveld naast "Spelers" label
            search_field = self.page.locator("input").filter(
                has=self.page.locator("xpath=preceding-sibling::*[contains(text(),'Spelers')]")
            ).first

        if search_field.is_visible():
            search_field.fill(speler)
            self.page.wait_for_timeout(500)

            # Klik op het zoekresultaat
            result = self.page.locator(
                f"li:has-text('{achternaam}'), "
                f".result:has-text('{achternaam}'), "
                f"[class*='suggestion']:has-text('{achternaam}'), "
                f"[class*='dropdown'] :has-text('{achternaam}')"
            ).first

            if result.is_visible():
                result.click()
                self.page.wait_for_timeout(300)
                self._log.info(f"Speler '{speler}' toegevoegd via zoekbalk")
                return True

        self._log.warning(f"Speler '{speler}' kon niet worden toegevoegd")
        self._screenshot(f"05_speler_niet_gevonden_{achternaam}", force=True)
        return False

    # =========================================================================
    # STAP 2: KIES EEN DAG
    # =========================================================================

    def _bepaal_dagdeel(self, tijden: list[str]) -> str:
        """
        Bepaal het dagdeel (Ochtend/Middag/Avond) op basis van de voorkeurtijden.

        Args:
            tijden: Lijst met gewenste tijden (bijv. ["20:30", "21:30"]).

        Returns:
            "Ochtend", "Middag" of "Avond".
        """
        if not tijden:
            return "Avond"

        # Gebruik de eerste tijd als referentie
        eerste_tijd = tijden[0]
        try:
            uur = int(eerste_tijd.split(":")[0])
        except (ValueError, IndexError):
            return "Avond"

        if uur < 12:
            return "Ochtend"
        elif uur < 17:
            return "Middag"
        else:
            return "Avond"

    def stap2_kies_dag(self, target_date: datetime, tijden: list[str] = None) -> bool:
        """
        Stap 2 van de wizard: Selecteer de gewenste datum en dagdeel.

        De pagina toont een weekoverzicht met kolommen per dag.
        Elke kolom heeft: dag-header (bijv. "do 12 februari") en drie periodes:
        Ochtend, Middag, Avond.

        Args:
            target_date: De datum waarvoor we willen reserveren.
            tijden: Lijst met voorkeurtijden om het dagdeel te bepalen.

        Returns:
            True als de datum en dagdeel succesvol geselecteerd zijn.
        """
        self._log.info(f"Stap 2: Kies een dag - {target_date.strftime('%A %d-%m-%Y')}")

        self.page.wait_for_timeout(300)
        self._screenshot("06_stap2_kies_dag")

        # Bepaal dag-afkorting en nummer zoals op de pagina getoond
        dag_afkortingen = {
            0: "ma", 1: "di", 2: "wo", 3: "do",
            4: "vr", 5: "za", 6: "zo"
        }
        dag_afk = dag_afkortingen[target_date.weekday()]
        dag_num = target_date.day
        dag_label = f"{dag_afk} {dag_num}"  # bijv. "do 12"

        # Bepaal dagdeel op basis van voorkeurtijden
        dagdeel = self._bepaal_dagdeel(tijden or [])
        self._log.info(f"Zoek naar: '{dag_label}' - dagdeel: '{dagdeel}'")

        try:
            # De pagina toont div.day kolommen met elk een header en dagdelen.
            # Zoek de kolom die de juiste dag bevat en klik op het juiste dagdeel.
            day_columns = self.page.locator(".day")
            column_count = day_columns.count()
            self._log.debug(f"Gevonden dag-kolommen: {column_count}")

            for i in range(column_count):
                column = day_columns.nth(i)
                column_text = column.text_content() or ""

                # Controleer of deze kolom de juiste dag bevat
                if dag_label in column_text.lower() or f"{dag_afk} {dag_num}" in column_text.lower():
                    self._log.info(f"Dag-kolom gevonden: kolom {i} ('{column_text[:30]}...')")

                    # Klik op het juiste dagdeel binnen deze kolom
                    dagdeel_element = column.locator(f"text='{dagdeel}'").first
                    if dagdeel_element.is_visible():
                        # Gebruik force=True om intercepted clicks te omzeilen
                        dagdeel_element.click(force=True)
                        self.page.wait_for_timeout(500)
                        self._screenshot("06b_dagdeel_geselecteerd")
                        self._log.info(f"Dagdeel '{dagdeel}' geselecteerd bij '{dag_label}'")

                        # Klik Volgende als die er is
                        volgende = self.page.locator(
                            "button:has-text('Volgende'), a:has-text('Volgende')"
                        ).first
                        if volgende.is_visible():
                            return self._klik_volgende("stap2")
                        return True
                    else:
                        self._log.warning(f"Dagdeel '{dagdeel}' niet beschikbaar bij '{dag_label}'")
                        self._screenshot("06_dagdeel_niet_beschikbaar", force=True)
                        return False

            # Als de dag niet in de huidige week zit, navigeer naar de juiste week
            self._log.info("Dag niet gevonden in huidige week, probeer week-navigatie...")
            next_week_btn = self.page.locator(
                "button:has-text('>'), a:has-text('>')"
            ).last  # De ">" knop naast de weekrange
            if next_week_btn.is_visible():
                next_week_btn.click()
                self.page.wait_for_timeout(500)
                self._screenshot("06c_volgende_week")
                # Recursief opnieuw proberen (1 keer)
                return self._selecteer_dagdeel_in_week(dag_label, dagdeel)

            self._log.error(f"Kon dag '{dag_label}' met dagdeel '{dagdeel}' niet vinden")
            self._screenshot("06_dag_niet_gevonden", force=True)
            return False

        except Exception as e:
            self._log.error(f"Fout bij dag selectie: {e}")
            self._screenshot("06_dag_fout", force=True)
            return False

    def _selecteer_dagdeel_in_week(self, dag_label: str, dagdeel: str) -> bool:
        """Zoek en selecteer een dagdeel in de huidige weekweergave."""
        day_columns = self.page.locator(".day")
        for i in range(day_columns.count()):
            column = day_columns.nth(i)
            column_text = (column.text_content() or "").lower()

            if dag_label in column_text:
                dagdeel_element = column.locator(f"text='{dagdeel}'").first
                if dagdeel_element.is_visible():
                    dagdeel_element.click(force=True)
                    self.page.wait_for_timeout(500)
                    self._screenshot("06d_dagdeel_gevonden")
                    self._log.info(f"Dagdeel '{dagdeel}' gevonden in volgende week")
                    volgende = self.page.locator(
                        "button:has-text('Volgende'), a:has-text('Volgende')"
                    ).first
                    if volgende.is_visible():
                        return self._klik_volgende("stap2")
                    return True

        self._log.error(f"Dag '{dag_label}' niet gevonden in volgende week")
        return False

    # =========================================================================
    # STAP 3: KIES EEN BAAN
    # =========================================================================

    def stap3_kies_baan(self, tijden: list[str], baan_voorkeur: list = None) -> dict | None:
        """
        Stap 3 van de wizard: Selecteer een baan en tijdslot.

        De pagina toont "Kies een baan" met een grid:
        - Rijen: Banen (bijv. "11 Padel 1 Kunstgras", "12 Padel 2 Padel", etc.)
        - Kolommen: Beschikbare tijdslots als klikbare elementen

        Strategie: gebruik JavaScript om de pagina-structuur te analyseren en
        vervolgens het juiste element te klikken.

        Args:
            tijden: Lijst met gewenste tijden in volgorde van voorkeur.
            baan_voorkeur: Optionele lijst met voorkeursbanen.

        Returns:
            Dict met geselecteerde baan en tijd, of None als niets beschikbaar is.
        """
        self._log.info(f"Stap 3: Kies een baan - tijden: {tijden}, voorkeur: {baan_voorkeur}")

        self.page.wait_for_timeout(300)
        self._screenshot("07_stap3_kies_baan")

        try:
            # Analyseer de pagina-structuur via JavaScript
            # Zoek alle klikbare tijdslot-elementen en hun bijbehorende baan
            slots_info = self.page.evaluate("""() => {
                const results = [];
                const timePattern = /^\\d{1,2}:\\d{2}$/;
                const courtPattern = /(?:Padel|Baan)\\s*\\d+/gi;
                const allElements = document.querySelectorAll('a, button, [role="button"], span, div');

                for (const el of allElements) {
                    const text = el.textContent.trim();
                    if (!timePattern.test(text) || el.offsetParent === null) continue;

                    // STRATEGIE: Loop omhoog door de DOM en zoek de KLEINSTE container
                    // die precies 1 uniek baan/court-naam bevat. Dat is de "baan-rij".
                    let baanNaam = '';
                    let current = el.parentElement;
                    for (let depth = 0; depth < 25 && current && current !== document.body; depth++) {
                        const txt = current.textContent || '';

                        // Zoek alle court-namen in deze container
                        const courtMatches = txt.match(courtPattern) || [];
                        const uniqueCourts = [...new Set(courtMatches.map(c => c.toLowerCase().replace(/\\s+/g, ' ').trim()))];

                        if (uniqueCourts.length === 1) {
                            // Precies 1 unieke baannaam -> dit is de juiste container
                            const m = courtMatches[0].match(/(?:Padel|Baan)\\s*(\\d+)/i);
                            if (m) {
                                const isPadel = courtMatches[0].toLowerCase().includes('padel');
                                baanNaam = (isPadel ? 'Padel ' : 'Baan ') + m[1];
                                break;
                            }
                        }

                        current = current.parentElement;
                    }

                    results.push({
                        tijd: text,
                        baan: baanNaam,
                        isPadel: baanNaam.toLowerCase().includes('padel'),
                        tagName: el.tagName,
                        index: Array.from(document.querySelectorAll(el.tagName)).indexOf(el)
                    });
                }
                return results;
            }""")

            self._log.info(f"Gevonden tijdslots: {len(slots_info)}")
            for slot in slots_info:
                self._log.debug(f"  Slot: {slot['tijd']} op {slot['baan']} (padel={slot['isPadel']})")

            # Filter op padel-banen
            padel_slots = [s for s in slots_info if s["isPadel"]]
            if not padel_slots:
                # Geen padel-banen beschikbaar - NIET terugvallen op tennisbanen
                if slots_info:
                    niet_padel_banen = set(s["baan"] for s in slots_info if s["baan"])
                    self._laatste_fout = (
                        f"Alle padelbanen zijn bezet. "
                        f"Er zijn alleen tennisbanen beschikbaar ({', '.join(niet_padel_banen) or 'onbekend'}). "
                        f"Gevraagde tijden: {', '.join(tijden)}"
                    )
                else:
                    self._laatste_fout = (
                        f"Geen enkele baan beschikbaar (ook geen tennis). "
                        f"Mogelijk zijn alle banen al gereserveerd voor de gevraagde tijden: {', '.join(tijden)}"
                    )
                self._log.warning(self._laatste_fout)
                self._screenshot("07_geen_padel_slots", force=True)
                return None

            # Probeer elke voorkeurstijd, per tijd de banen in voorkeursvolgorde
            geprobeerde_tijden = []
            for tijd in tijden:
                matching = [s for s in padel_slots if s["tijd"] == tijd]
                if not matching:
                    geprobeerde_tijden.append(tijd)
                    self._log.warning(f"Tijdslot {tijd} niet beschikbaar op padel-banen")
                    continue

                # Sorteer op baanvoorkeur als die is opgegeven
                if baan_voorkeur:
                    slot = self._kies_baan_op_voorkeur(matching, baan_voorkeur)
                else:
                    slot = matching[0]

                if slot:
                    self._log.info(f"Match gevonden: {tijd} op {slot['baan']}")
                    self._laatste_fout = None  # Reset fout bij succes
                    self._klik_tijdslot_op_padel(tijd, slot["baan"])
                    return {"tijd": tijd, "baan": slot["baan"]}

            # Geen enkel gewenst tijdslot beschikbaar op padelbanen
            beschikbare_tijden = sorted(set(s["tijd"] for s in padel_slots))
            beschikbare_banen = sorted(set(s["baan"] for s in padel_slots))
            self._laatste_fout = (
                f"Geen padelbaan vrij op de gewenste tijden ({', '.join(tijden)}). "
                f"Wel beschikbaar: {', '.join(beschikbare_tijden)} op {', '.join(beschikbare_banen)}"
            ) if beschikbare_tijden else (
                f"Geen padelbaan beschikbaar op de gewenste tijden ({', '.join(tijden)})"
            )
            self._log.error(self._laatste_fout)
            self._screenshot("07_geen_slots", force=True)
            return None

        except Exception as e:
            self._laatste_fout = f"Technische fout bij baan selectie: {e}"
            self._log.error(self._laatste_fout)
            self._screenshot("07_baan_fout", force=True)
            return None

    def _extract_baan_nummer(self, baan_naam: str) -> int | None:
        """
        Haal het baannummer uit een baannaam zoals '11 Padel 1 Kunstgras'.
        Zoekt naar het getal direct na 'Padel' of 'Baan'.

        Returns:
            Het baannummer als int, of None.
        """
        import re
        match = re.search(r'(?:padel|baan)\s*(\d+)', baan_naam, re.IGNORECASE)
        if match:
            return int(match.group(1))
        return None

    def _kies_baan_op_voorkeur(self, slots: list[dict], baan_voorkeur: list[int]) -> dict | None:
        """
        Kies het beste slot op basis van de baanvoorkeur-volgorde.

        Doorloopt de baan_voorkeur lijst (bijv. [1, 3, 4, 2]) en
        retourneert het eerste slot dat op een voorkeursbaan beschikbaar is.

        Args:
            slots: Lijst met beschikbare slots (elk met 'baan' key).
            baan_voorkeur: Lijst met baannummers in volgorde van voorkeur.

        Returns:
            Het beste beschikbare slot, of het eerste slot als geen voorkeur matcht.
        """
        for voorkeur_nr in baan_voorkeur:
            for slot in slots:
                baan_nr = self._extract_baan_nummer(slot["baan"])
                if baan_nr == voorkeur_nr:
                    self._log.info(f"Baan {voorkeur_nr} beschikbaar: {slot['baan']}")
                    return slot
                    
            self._log.debug(f"Baan {voorkeur_nr} niet beschikbaar")

        # Geen enkele voorkeursbaan beschikbaar, neem de eerste
        self._log.warning(f"Geen voorkeursbaan ({baan_voorkeur}) beschikbaar, "
                       f"neem eerste: {slots[0]['baan']}")
        return slots[0] if slots else None

    def _klik_tijdslot_op_padel(self, tijd: str, baan_naam: str):
        """
        Klik op een specifiek tijdslot bij een specifieke (padel)baan.

        Markeert het element via JavaScript met een data-attribuut,
        dan klikt via Playwright's native click (triggert alle event handlers).
        """
        self._log.info(f"Klik op tijdslot {tijd} bij {baan_naam}")

        # Stap 1: Gebruik JavaScript om het juiste element te markeren
        marked = self.page.evaluate(f"""() => {{
            // Verwijder eventuele oude markers
            document.querySelectorAll('[data-bot-target]').forEach(
                el => el.removeAttribute('data-bot-target')
            );

            const allElements = document.querySelectorAll('a, button, [role="button"], span, td, div');

            // Zoek eerst bij de juiste baan
            if ('{baan_naam}') {{
                for (const el of allElements) {{
                    const text = el.textContent.trim();
                    if (text === '{tijd}' && el.offsetParent !== null) {{
                        let parent = el.parentElement;
                        for (let i = 0; i < 15 && parent; i++) {{
                            if (parent.textContent && parent.textContent.includes('{baan_naam}')) {{
                                el.setAttribute('data-bot-target', 'true');
                                return true;
                            }}
                            parent = parent.parentElement;
                        }}
                    }}
                }}
            }}

            // Fallback: markeer de eerste zichtbare match
            for (const el of allElements) {{
                if (el.textContent.trim() === '{tijd}' && el.offsetParent !== null) {{
                    el.setAttribute('data-bot-target', 'true');
                    return true;
                }}
            }}
            return false;
        }}""")

        if not marked:
            self._log.error(f"Kon tijdslot {tijd} niet vinden op de pagina")
            self._screenshot("07_klik_niet_gevonden", force=True)
            return

        # Stap 2: Klik via Playwright (triggert alle event handlers correct)
        target = self.page.locator("[data-bot-target='true']").first
        if target.is_visible():
            target.click()
            self.page.wait_for_timeout(500)
            self._screenshot(f"07b_{tijd}_geklikt")
            self._log.info(f"Tijdslot {tijd} aangeklikt via Playwright")

            # Klik Volgende als die er is
            volgende = self.page.locator(
                "button:has-text('Volgende'), a:has-text('Volgende')"
            ).first
            if volgende.is_visible():
                self._klik_volgende("stap3")
        else:
            self._log.error(f"Gemarkeerd element voor {tijd} is niet zichtbaar")
            self._screenshot("07_target_niet_zichtbaar", force=True)

    # =========================================================================
    # STAP 4: BEVESTIGEN
    # =========================================================================

    def stap4_bevestigen(self, dry_run: bool = False) -> tuple[bool, str]:
        """
        Stap 4 van de wizard: Bevestig de reservering.

        Args:
            dry_run: Als True, wordt de reservering niet daadwerkelijk bevestigd.

        Returns:
            Tuple van (succes: bool, detail: str).
            Bij succes is detail een bevestigingsmelding.
            Bij falen is detail een specifieke foutomschrijving.
        """
        self._log.info(f"Stap 4: Bevestigen (dry_run={dry_run})")

        self.page.wait_for_timeout(300)
        self._screenshot("08_stap4_bevestigen")

        if dry_run:
            self._log.info("DRY RUN - Reservering wordt NIET bevestigd")
            return (True, "DRY RUN - niet daadwerkelijk gereserveerd")

        try:
            confirm_selectors = [
                "button:has-text('Bevestigen')",
                "button:has-text('Bevestig')",
                "button:has-text('Reserveren')",
                "button:has-text('Reserveer')",
                "button:has-text('Afhangen')",
                "a:has-text('Bevestigen')",
                "a:has-text('Reserveren')",
                "input[type='submit']",
            ]

            for selector in confirm_selectors:
                button = self.page.locator(selector).first
                if button.is_visible() and button.is_enabled():
                    self._log.info(f"Bevestigingsknop gevonden: {selector}")
                    pre_url = self.page.url
                    button.click()
                    # Wacht op pagina-update: eerst snel domcontentloaded,
                    # dan kort wachten op eventuele redirect/content-update
                    try:
                        self.page.wait_for_load_state("networkidle", timeout=5000)
                    except PlaywrightTimeout:
                        pass
                    # Extra korte wacht als URL niet veranderd is
                    if self.page.url == pre_url:
                        self.page.wait_for_timeout(500)
                    self._screenshot("08b_na_bevestiging", force=True)
                    return self._check_bevestiging()

            self._log.error("Geen bevestigingsknop gevonden")
            self._screenshot("08_geen_bevestigingsknop", force=True)
            return (False, "Geen bevestigingsknop gevonden op de pagina")

        except Exception as e:
            self._log.error(f"Fout bij bevestiging: {e}")
            self._screenshot("08_bevestiging_fout", force=True)
            return (False, f"Technische fout bij bevestiging: {e}")

    # =========================================================================
    # HULP-FUNCTIES
    # =========================================================================

    def _klik_volgende(self, stap_naam: str) -> bool:
        """Klik op de 'Volgende >' knop om naar de volgende stap te gaan."""
        self._log.info(f"Klik op 'Volgende' ({stap_naam})")
        try:
            volgende_btn = self.page.locator(
                "button:has-text('Volgende'), "
                "a:has-text('Volgende')"
            ).first

            if volgende_btn.is_visible() and volgende_btn.is_enabled():
                volgende_btn.click()
                self.page.wait_for_load_state("domcontentloaded", timeout=10000)
                self._screenshot(f"{stap_naam}_na_volgende")
                self._log.info(f"Doorgegaan naar volgende stap ({stap_naam})")
                return True
            else:
                self._log.warning(f"'Volgende' knop niet zichtbaar of niet klikbaar ({stap_naam})")
                self._screenshot(f"{stap_naam}_volgende_niet_gevonden", force=True)
                return False
        except Exception as e:
            self._log.error(f"Fout bij klikken op Volgende ({stap_naam}): {e}")
            return False

    def _check_bevestiging(self) -> tuple[bool, str]:
        """
        Controleer of de reservering succesvol is bevestigd.

        Returns:
            Tuple van (succes: bool, detail: str).
            Bij succes is detail een bevestigingsmelding.
            Bij falen is detail een specifieke foutomschrijving.
        """
        # Gebruik visible text (niet hidden elements / scripts / class names)
        page_text = self.page.evaluate("""() => {
            return document.body.innerText.toLowerCase();
        }""") or ""
        current_url = self.page.url.lower()

        success_indicators = [
            "reservering geplaatst", "reservering bevestigd",
            "succesvol gereserveerd", "baan gereserveerd",
            "gelukt", "bevestigd",
        ]

        # Specifieke foutpatronen met duidelijke beschrijvingen
        # Volgorde is belangrijk: meer specifiek eerst
        error_patterns = [
            {
                "indicators": ["heeft al een reservering", "already has a reservation",
                               "al een reservering", "already reserved"],
                "beschrijving": "Speler heeft al een reservering",
                "detail_fn": self._extract_speler_fout,
                "retry": False,  # Geen zin om te retrien
            },
            {
                "indicators": ["maximaal", "maximum", "limiet"],
                "beschrijving": "Maximaal aantal reserveringen bereikt",
                "detail_fn": None,
                "retry": False,
            },
            {
                "indicators": ["bezet", "niet beschikbaar", "al gereserveerd",
                               "occupied", "not available"],
                "beschrijving": "Baan is bezet of niet meer beschikbaar",
                "detail_fn": None,
                "retry": True,
            },
            {
                "indicators": ["maak een keuze", "selecteer een"],
                "beschrijving": "Geen baan of tijdslot geselecteerd (validatiefout)",
                "detail_fn": None,
                "retry": True,
            },
            {
                "indicators": ["niet toegestaan", "kan niet", "niet mogelijk"],
                "beschrijving": "Reservering niet toegestaan",
                "detail_fn": None,
                "retry": False,
            },
            {
                "indicators": ["fout", "error", "mislukt", "failed"],
                "beschrijving": "Er is een fout opgetreden bij het reserveren",
                "detail_fn": None,
                "retry": True,
            },
        ]

        # Check succes-indicatoren
        for indicator in success_indicators:
            if indicator in page_text:
                self._log.info(f"Bevestiging gevonden: '{indicator}'")
                return (True, f"Reservering bevestigd ({indicator})")

        # Check fout-indicatoren met specifieke berichten
        for pattern in error_patterns:
            for indicator in pattern["indicators"]:
                if indicator in page_text:
                    # Probeer specifieke details te extraheren
                    detail = pattern["beschrijving"]
                    if pattern["detail_fn"]:
                        extra = pattern["detail_fn"](page_text)
                        if extra:
                            detail = extra

                    # Extraheer relevante tekst rondom de fout
                    context = self._extract_fout_context(page_text, indicator)
                    if context and context != detail:
                        detail = f"{detail}: \"{context}\""

                    self._log.warning(f"Foutmelding: {detail}")
                    self._laatste_fout = detail
                    return (False, detail)

        # Check of we op de bevestigingspagina zijn (expliciete succes-URL)
        if "reservationsconfirm" in current_url:
            self._log.info(f"Bevestigingspagina bereikt (URL: {current_url}) - reservering gelukt")
            return (True, "Reservering bevestigd (bevestigingspagina bereikt)")

        # Check of de wizard is verlaten (redirect naar hoofdmenu = succes)
        if "reservationsplayers" not in current_url:
            self._log.info(f"Wizard verlaten (URL: {current_url}) - reservering waarschijnlijk gelukt")
            return (True, "Reservering waarschijnlijk gelukt (wizard verlaten)")

        # Nog steeds op de wizard-pagina zonder duidelijke indicator
        self._log.warning("Geen duidelijke bevestiging gevonden - controleer screenshots")
        self._log.debug(f"URL: {current_url}")
        self._log.debug(f"Pagina tekst (eerste 500 tekens): {page_text[:500]}")
        return (False, "Geen bevestiging ontvangen - controleer handmatig of de reservering is geplaatst")

    def _extract_speler_fout(self, page_text: str) -> str | None:
        """
        Probeer de naam van de speler te extraheren die al een reservering heeft.

        Zoekt naar patronen als:
        - "Ruud van Erp heeft al een reservering"
        - "Ron Spaans already has a reservation"
        """
        import re
        # Nederlands patroon
        match = re.search(r'([A-Z][a-zà-ü]+(?: [a-zà-ü]+)*(?: [A-Z][a-zà-ü]+)+)\s+heeft al een reservering',
                          page_text, re.IGNORECASE)
        if match:
            naam = match.group(1).strip()
            return f"{naam} heeft al een reservering in dit tijdvak (2 uur voor/na)"

        # Engels patroon
        match = re.search(r'([A-Z][a-zà-ü]+(?: [a-zà-ü]+)*(?: [A-Z][a-zà-ü]+)+)\s+already',
                          page_text, re.IGNORECASE)
        if match:
            naam = match.group(1).strip()
            return f"{naam} heeft al een reservering in dit tijdvak (2 uur voor/na)"

        return "Een van de spelers heeft al een reservering in dit tijdvak (2 uur voor/na)"

    def _extract_fout_context(self, page_text: str, indicator: str) -> str | None:
        """
        Extraheer een kort stuk tekst rondom een gevonden fout-indicator.

        Zoekt de indicator in de tekst en retourneert de zin (of een deel ervan)
        waar deze in voorkomt.
        """
        try:
            idx = page_text.index(indicator)
            # Pak 100 tekens voor en na de indicator
            start = max(0, idx - 80)
            end = min(len(page_text), idx + len(indicator) + 80)
            snippet = page_text[start:end].strip()

            # Probeer op zin-grenzen te knippen
            # Zoek het begin van de zin
            for sep in ['\n', '. ', '! ']:
                last_sep = snippet[:idx - start].rfind(sep)
                if last_sep != -1:
                    snippet = snippet[last_sep + len(sep):]
                    break

            # Zoek het einde van de zin
            for sep in ['\n', '. ', '! ']:
                next_sep = snippet.find(sep, len(indicator))
                if next_sep != -1:
                    snippet = snippet[:next_sep]
                    break

            snippet = snippet.strip()
            if len(snippet) > 150:
                snippet = snippet[:150] + "..."

            return snippet if snippet else None
        except (ValueError, IndexError):
            return None

    # =========================================================================
    # HOOFD-FLOW (gesplitst in voorbereiden + probeer_reserveer)
    # =========================================================================

    def voorbereiden(
        self,
        target_date: datetime,
        tijden: list[str],
        spelers: list[str],
    ) -> str | None:
        """
        Bereid de reservering voor: login + navigatie (gecombineerd), stap 1 (spelers), stap 2 (dag).

        Navigeert direct naar de reserverings-URL. De site redirect naar login
        als je niet ingelogd bent. Na login ben je direct op de reserveringspagina.
        Dit bespaart een extra navigatie-stap (~2-3s).

        Args:
            target_date: Datum waarvoor gereserveerd moet worden.
            tijden: Lijst met voorkeurtijden (om dagdeel te bepalen).
            spelers: Lijst met medespelers.

        Returns:
            None bij succes, foutmelding-string bij falen.
        """
        try:
            urls = self.config["urls"]
            reservering_url = urls["reservering"]

            # Combineer login + navigatie: ga direct naar reserverings-URL
            if not self.login(target_url=reservering_url):
                return "Login mislukt"

            # Controleer of we na login op de reserveringspagina zijn
            current_url = self.page.url.lower()
            if "reservationsplayers" in current_url:
                self._log.info("Direct op reserveringspagina na login")
                # Check of stap 1 zichtbaar is
                stap1 = self.page.locator("text=Partners kiezen")
                if stap1.is_visible():
                    self._log.info("Stap 1: Partners kiezen is zichtbaar")
                else:
                    self._log.info("Pagina geladen, maar stap 1 niet zichtbaar - probeer navigatie")
                    if not self.navigeer_naar_reservering():
                        return "Kon niet naar reserveringspagina navigeren"
            else:
                # Na login zijn we niet op de reserveringspagina, navigeer er apart naartoe
                self._log.info("Na login niet op reserveringspagina - navigeer apart")
                if not self.navigeer_naar_reservering():
                    return "Kon niet naar reserveringspagina navigeren"

            if not self.stap1_partners_kiezen(spelers):
                return "Kon medespelers niet toevoegen (stap 1)"

            if not self.stap2_kies_dag(target_date, tijden=tijden):
                return f"Kon datum {target_date.strftime('%d-%m-%Y')} niet selecteren (stap 2)"

            self._log.info("Voorbereiding klaar - klaarstaan op stap 3 (kies een baan)")
            return None

        except ReserveringError as e:
            return str(e)
        except Exception as e:
            return f"Onverwachte fout bij voorbereiding: {e}"

    def probeer_reserveer(
        self,
        target_date: datetime,
        tijden: list[str],
        spelers: list[str],
        baan_voorkeur: list = None,
        dry_run: bool = False,
        is_eerste_poging: bool = False,
    ) -> dict:
        """
        Probeer de volledige reservering uit te voeren.

        Bij de eerste poging staat de browser al op stap 3 (na voorbereiden).
        Bij vervolgpogingen wordt de wizard opnieuw doorlopen.

        Returns:
            Dict met: success (bool), tijd, baan, foutmelding, en retry (bool).
            retry=True betekent: probeer opnieuw (slot nog niet beschikbaar).
            retry=False betekent: klaar (succes of definitief mislukt).
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
                # Navigeer opnieuw door de wizard (stap 1 + 2) om op stap 3 te komen
                self._log.info("Herstart wizard voor nieuwe poging...")
                if not self.navigeer_naar_reservering():
                    result["foutmelding"] = "Kon niet naar reserveringspagina navigeren"
                    result["retry"] = True
                    return result

                if not self.stap1_partners_kiezen(spelers):
                    result["foutmelding"] = "Kon spelers niet toevoegen bij retry"
                    result["retry"] = True
                    return result

                if not self.stap2_kies_dag(target_date, tijden=tijden):
                    result["foutmelding"] = "Kon dag niet selecteren bij retry"
                    result["retry"] = True
                    return result

            slot = self.stap3_kies_baan(tijden, baan_voorkeur)
            if not slot:
                result["foutmelding"] = self._laatste_fout or "Geen beschikbaar padel-tijdslot gevonden"
                result["retry"] = True
                return result

            result["tijd"] = slot["tijd"]
            result["baan"] = slot["baan"]

            succes, detail = self.stap4_bevestigen(dry_run=dry_run)
            if succes:
                result["success"] = True
                if dry_run:
                    result["foutmelding"] = detail
            else:
                result["foutmelding"] = detail
                # Bepaal of retry zin heeft op basis van het type fout
                # Bij "speler heeft al een reservering" of "maximaal bereikt" heeft retry geen zin
                fout_lower = detail.lower() if detail else ""
                no_retry_indicators = [
                    "heeft al een reservering",
                    "maximaal",
                    "niet toegestaan",
                ]
                result["retry"] = not any(ind in fout_lower for ind in no_retry_indicators)

        except Exception as e:
            result["foutmelding"] = f"Fout: {e}"
            result["retry"] = True
            self._log.error(f"Fout bij reserveerpoging: {e}")

        return result

    def reserveer(
        self,
        target_date: datetime,
        tijden: list[str],
        spelers: list[str],
        baan_voorkeur: list = None,
        dry_run: bool = False,
    ) -> dict:
        """
        Voer het volledige reserveringsproces uit (zonder retry-loop).
        Wordt gebruikt voor lokaal testen en eenvoudige runs.
        """
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
        finally:
            self._screenshot("09_eindresultaat", force=True)

        return result

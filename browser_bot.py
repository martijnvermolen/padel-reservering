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

    def __init__(self, config: dict):
        self.config = config
        self.browser: Browser | None = None
        self.page: Page | None = None
        self.playwright = None
        self._ensure_screenshots_dir()

    def _ensure_screenshots_dir(self):
        """Maak screenshots directory aan als die niet bestaat."""
        SCREENSHOTS_DIR.mkdir(exist_ok=True)

    def _screenshot(self, name: str):
        """Maak een screenshot voor debugging."""
        if self.page:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = SCREENSHOTS_DIR / f"{timestamp}_{name}.png"
            try:
                self.page.screenshot(path=str(path), full_page=True)
                logger.debug(f"Screenshot opgeslagen: {path}")
            except Exception as e:
                logger.warning(f"Kon screenshot niet maken: {e}")

    def start(self):
        """Start de browser."""
        browser_config = self.config.get("browser", {})
        headless = browser_config.get("headless", True)
        slow_mo = browser_config.get("slow_mo", 0)
        timeout = browser_config.get("timeout", 30000)

        logger.info(f"Browser starten (headless={headless}, slow_mo={slow_mo}ms)")

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
        logger.info("Browser gesloten")

    # =========================================================================
    # LOGIN
    # =========================================================================

    def login(self) -> bool:
        """
        Log in op het KNLTB reserveringssysteem.

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

        logger.info(f"Inloggen met {login_type}: {username}")

        try:
            self.page.goto(urls["login"], wait_until="networkidle")
            self._screenshot("01_login_pagina")

            # Selecteer login type als nodig
            if login_type == "clublidnummer":
                clubnr_option = self.page.locator("text=Clublidnummer")
                if clubnr_option.is_visible():
                    clubnr_option.click()
                    self.page.wait_for_timeout(500)

            # Vul gebruikersnaam in
            username_field = self.page.locator(
                "input[type='text'], input[type='number'], "
                "input[name*='user'], input[name*='login'], "
                "input[name*='bonds'], input[placeholder*='ummer']"
            ).first
            username_field.fill(str(username))
            logger.debug("Gebruikersnaam ingevuld")

            # Vul wachtwoord in
            password_field = self.page.locator("input[type='password']").first
            password_field.fill(password)
            logger.debug("Wachtwoord ingevuld")

            self._screenshot("02_login_ingevuld")

            # Klik op inloggen
            login_button = self.page.locator(
                "button:has-text('Log in'), input[type='submit']:has-text('Log in'), "
                "button:has-text('Inloggen'), a:has-text('Log in')"
            ).first
            login_button.click()

            self.page.wait_for_load_state("networkidle")
            self.page.wait_for_timeout(2000)
            self._screenshot("03_na_login")

            if self._is_logged_in():
                logger.info("Login succesvol!")
                return True
            else:
                logger.error("Login mislukt - nog steeds op login pagina")
                self._screenshot("03_login_mislukt")
                return False

        except PlaywrightTimeout as e:
            logger.error(f"Timeout tijdens inloggen: {e}")
            self._screenshot("03_login_timeout")
            return False
        except Exception as e:
            logger.error(f"Fout tijdens inloggen: {e}")
            self._screenshot("03_login_fout")
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
        logger.info("Navigeren naar reserveringspagina...")

        try:
            self.page.goto(urls["reservering"], wait_until="networkidle")
            self.page.wait_for_timeout(2000)
            self._screenshot("04_reservering_pagina")

            # Controleer of we op stap 1 (Partners kiezen) zijn
            stap1 = self.page.locator("text=Partners kiezen")
            if stap1.is_visible():
                logger.info("Reserveringspagina geladen - Stap 1: Partners kiezen")
                return True

            # Probeer op "Baan reserveringen" tab te klikken
            baan_tab = self.page.locator(
                "a:has-text('Baan reserveringen'), "
                "a:has-text('Baan reserveren')"
            ).first
            if baan_tab.is_visible():
                baan_tab.click()
                self.page.wait_for_load_state("networkidle")
                self.page.wait_for_timeout(2000)
                self._screenshot("04b_baan_tab_geklikt")

            logger.info(f"Huidige URL: {self.page.url}")
            return True

        except Exception as e:
            logger.error(f"Fout bij navigatie: {e}")
            self._screenshot("04_navigatie_fout")
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
            logger.warning("Geen medespelers opgegeven")
            return False

        logger.info(f"Stap 1: Partners kiezen - {spelers}")
        spelers_toegevoegd = 0

        for speler in spelers:
            if self._voeg_speler_toe(speler):
                spelers_toegevoegd += 1

        self._screenshot("05_spelers_toegevoegd")
        logger.info(f"{spelers_toegevoegd}/{len(spelers)} spelers toegevoegd")

        if spelers_toegevoegd == 0:
            logger.error("Geen enkele speler toegevoegd")
            return False

        # Klik op "Volgende >" om naar stap 2 te gaan
        return self._klik_volgende("stap1")

    def _voeg_speler_toe(self, speler: str) -> bool:
        """
        Voeg een enkele speler toe.

        Probeert eerst via "Recent mee gespeeld" (klik op "+" naast de naam),
        daarna via het zoekveld.
        """
        logger.info(f"Speler toevoegen: {speler}")

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
                self.page.wait_for_timeout(800)
                logger.info(f"Speler '{speler}' toegevoegd via Recent mee gespeeld (+)")
                return True

            # Probeer het hele parent element te klikken (de kaart zelf)
            try:
                parent.click()
                self.page.wait_for_timeout(800)
                logger.info(f"Speler '{speler}' toegevoegd via klik op kaart")
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
                    self.page.wait_for_timeout(800)
                    logger.info(f"Speler '{speler}' toegevoegd via achternaam '{achternaam}'")
                    return True
                except Exception:
                    pass

        # Methode 3: Gebruik het zoekveld "Spelers"
        logger.info(f"Speler '{speler}' niet in Recent - probeer zoekbalk")
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
            self.page.wait_for_timeout(1500)

            # Klik op het zoekresultaat
            result = self.page.locator(
                f"li:has-text('{achternaam}'), "
                f".result:has-text('{achternaam}'), "
                f"[class*='suggestion']:has-text('{achternaam}'), "
                f"[class*='dropdown'] :has-text('{achternaam}')"
            ).first

            if result.is_visible():
                result.click()
                self.page.wait_for_timeout(800)
                logger.info(f"Speler '{speler}' toegevoegd via zoekbalk")
                return True

        logger.warning(f"Speler '{speler}' kon niet worden toegevoegd")
        self._screenshot(f"05_speler_niet_gevonden_{achternaam}")
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
        logger.info(f"Stap 2: Kies een dag - {target_date.strftime('%A %d-%m-%Y')}")

        self.page.wait_for_timeout(1000)
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
        logger.info(f"Zoek naar: '{dag_label}' - dagdeel: '{dagdeel}'")

        try:
            # De pagina toont div.day kolommen met elk een header en dagdelen.
            # Zoek de kolom die de juiste dag bevat en klik op het juiste dagdeel.
            day_columns = self.page.locator(".day")
            column_count = day_columns.count()
            logger.debug(f"Gevonden dag-kolommen: {column_count}")

            for i in range(column_count):
                column = day_columns.nth(i)
                column_text = column.text_content() or ""

                # Controleer of deze kolom de juiste dag bevat
                if dag_label in column_text.lower() or f"{dag_afk} {dag_num}" in column_text.lower():
                    logger.info(f"Dag-kolom gevonden: kolom {i} ('{column_text[:30]}...')")

                    # Klik op het juiste dagdeel binnen deze kolom
                    dagdeel_element = column.locator(f"text='{dagdeel}'").first
                    if dagdeel_element.is_visible():
                        # Gebruik force=True om intercepted clicks te omzeilen
                        dagdeel_element.click(force=True)
                        self.page.wait_for_timeout(1500)
                        self._screenshot("06b_dagdeel_geselecteerd")
                        logger.info(f"Dagdeel '{dagdeel}' geselecteerd bij '{dag_label}'")

                        # Klik Volgende als die er is
                        volgende = self.page.locator(
                            "button:has-text('Volgende'), a:has-text('Volgende')"
                        ).first
                        if volgende.is_visible():
                            return self._klik_volgende("stap2")
                        return True
                    else:
                        logger.warning(f"Dagdeel '{dagdeel}' niet beschikbaar bij '{dag_label}'")
                        # Probeer een ander dagdeel
                        for alt_dagdeel in ["Avond", "Middag", "Ochtend"]:
                            if alt_dagdeel == dagdeel:
                                continue
                            alt_element = column.locator(f"text='{alt_dagdeel}'").first
                            if alt_element.is_visible():
                                alt_element.click(force=True)
                                self.page.wait_for_timeout(1500)
                                self._screenshot(f"06b_{alt_dagdeel}_geselecteerd")
                                logger.info(f"Alternatief dagdeel '{alt_dagdeel}' geselecteerd")
                                volgende = self.page.locator(
                                    "button:has-text('Volgende'), a:has-text('Volgende')"
                                ).first
                                if volgende.is_visible():
                                    return self._klik_volgende("stap2")
                                return True

            # Als de dag niet in de huidige week zit, navigeer naar de juiste week
            logger.info("Dag niet gevonden in huidige week, probeer week-navigatie...")
            next_week_btn = self.page.locator(
                "button:has-text('>'), a:has-text('>')"
            ).last  # De ">" knop naast de weekrange
            if next_week_btn.is_visible():
                next_week_btn.click()
                self.page.wait_for_timeout(2000)
                self._screenshot("06c_volgende_week")
                # Recursief opnieuw proberen (1 keer)
                return self._selecteer_dagdeel_in_week(dag_label, dagdeel)

            logger.error(f"Kon dag '{dag_label}' met dagdeel '{dagdeel}' niet vinden")
            self._screenshot("06_dag_niet_gevonden")
            return False

        except Exception as e:
            logger.error(f"Fout bij dag selectie: {e}")
            self._screenshot("06_dag_fout")
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
                    self.page.wait_for_timeout(1500)
                    self._screenshot("06d_dagdeel_gevonden")
                    logger.info(f"Dagdeel '{dagdeel}' gevonden in volgende week")
                    volgende = self.page.locator(
                        "button:has-text('Volgende'), a:has-text('Volgende')"
                    ).first
                    if volgende.is_visible():
                        return self._klik_volgende("stap2")
                    return True

        logger.error(f"Dag '{dag_label}' niet gevonden in volgende week")
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
        logger.info(f"Stap 3: Kies een baan - tijden: {tijden}, voorkeur: {baan_voorkeur}")

        self.page.wait_for_timeout(1000)
        self._screenshot("07_stap3_kies_baan")

        try:
            # Analyseer de pagina-structuur via JavaScript
            # Zoek alle klikbare tijdslot-elementen en hun bijbehorende baan
            slots_info = self.page.evaluate("""() => {
                const results = [];
                // Zoek alle links/knoppen die een tijd bevatten (bijv. "21:30")
                const timePattern = /^\\d{1,2}:\\d{2}$/;
                const allElements = document.querySelectorAll('a, button, [role="button"], span, div');
                for (const el of allElements) {
                    const text = el.textContent.trim();
                    if (timePattern.test(text) && el.offsetParent !== null) {
                        // Zoek de baan-naam door omhoog te navigeren in de DOM
                        let baanNaam = '';
                        let parent = el.closest('[class*="court"], [class*="row"], tr, [class*="baan"]');
                        if (!parent) {
                            // Zoek verder omhoog
                            parent = el.parentElement;
                            for (let i = 0; i < 10 && parent; i++) {
                                const parentText = parent.textContent || '';
                                if (parentText.toLowerCase().includes('padel') ||
                                    parentText.toLowerCase().includes('baan')) {
                                    break;
                                }
                                parent = parent.parentElement;
                            }
                        }
                        if (parent) {
                            // Zoek de baan-naam in de buurt
                            const baanEl = parent.querySelector('[class*="name"], [class*="title"], strong, b, h3, h4');
                            if (baanEl) {
                                baanNaam = baanEl.textContent.trim();
                            }
                            if (!baanNaam) {
                                // Neem de eerste tekst die Padel of Baan bevat
                                const allText = parent.textContent;
                                const match = allText.match(/(\\d+\\s*Padel\\s*\\d+|\\d+\\s*Baan\\s*\\d+)/i);
                                if (match) baanNaam = match[1];
                            }
                        }
                        results.push({
                            tijd: text,
                            baan: baanNaam,
                            isPadel: baanNaam.toLowerCase().includes('padel'),
                            // Maak een unieke selector voor dit element
                            tagName: el.tagName,
                            index: Array.from(document.querySelectorAll(el.tagName)).indexOf(el)
                        });
                    }
                }
                return results;
            }""")

            logger.info(f"Gevonden tijdslots: {len(slots_info)}")
            for slot in slots_info:
                logger.debug(f"  Slot: {slot['tijd']} op {slot['baan']} (padel={slot['isPadel']})")

            # Filter op padel-banen
            padel_slots = [s for s in slots_info if s["isPadel"]]
            if not padel_slots:
                logger.warning("Geen padel-slots gevonden, gebruik alle slots")
                padel_slots = slots_info

            # Probeer elke voorkeurstijd
            for tijd in tijden:
                matching = [s for s in padel_slots if s["tijd"] == tijd]
                if matching:
                    # Neem de eerste match (of filter op baan_voorkeur)
                    slot = matching[0]
                    logger.info(f"Match gevonden: {tijd} op {slot['baan']}")

                    # Klik op het element via de tag + index
                    selector = f"{slot['tagName'].lower()}:nth-of-type({slot['index'] + 1})"
                    # Gebruik een directere methode: zoek de tijd-tekst binnen padel-context
                    self._klik_tijdslot_op_padel(tijd, slot["baan"])
                    return {"tijd": tijd, "baan": slot["baan"]}

                logger.warning(f"Tijdslot {tijd} niet beschikbaar op padel-banen")

            # Fallback: eerste beschikbare padel-slot
            if padel_slots:
                slot = padel_slots[0]
                logger.info(f"Fallback: {slot['tijd']} op {slot['baan']}")
                self._klik_tijdslot_op_padel(slot["tijd"], slot["baan"])
                return {"tijd": slot["tijd"], "baan": slot["baan"]}

            logger.error("Geen beschikbaar tijdslot gevonden")
            self._screenshot("07_geen_slots")
            return None

        except Exception as e:
            logger.error(f"Fout bij baan selectie: {e}")
            self._screenshot("07_baan_fout")
            return None

    def _klik_tijdslot_op_padel(self, tijd: str, baan_naam: str):
        """
        Klik op een specifiek tijdslot bij een specifieke (padel)baan.

        Markeert het element via JavaScript met een data-attribuut,
        dan klikt via Playwright's native click (triggert alle event handlers).
        """
        logger.info(f"Klik op tijdslot {tijd} bij {baan_naam}")

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
            logger.error(f"Kon tijdslot {tijd} niet vinden op de pagina")
            self._screenshot("07_klik_niet_gevonden")
            return

        # Stap 2: Klik via Playwright (triggert alle event handlers correct)
        target = self.page.locator("[data-bot-target='true']").first
        if target.is_visible():
            target.click()
            self.page.wait_for_timeout(2000)
            self._screenshot(f"07b_{tijd}_geklikt")
            logger.info(f"Tijdslot {tijd} aangeklikt via Playwright")

            # Controleer of er een selectie is gemaakt (visuele feedback)
            # Wacht even en maak screenshot
            self._screenshot(f"07c_{tijd}_na_klik")

            # Klik Volgende als die er is
            volgende = self.page.locator(
                "button:has-text('Volgende'), a:has-text('Volgende')"
            ).first
            if volgende.is_visible():
                self._klik_volgende("stap3")
        else:
            logger.error(f"Gemarkeerd element voor {tijd} is niet zichtbaar")
            self._screenshot("07_target_niet_zichtbaar")

    # =========================================================================
    # STAP 4: BEVESTIGEN
    # =========================================================================

    def stap4_bevestigen(self, dry_run: bool = False) -> bool:
        """
        Stap 4 van de wizard: Bevestig de reservering.

        Args:
            dry_run: Als True, wordt de reservering niet daadwerkelijk bevestigd.

        Returns:
            True als de reservering succesvol is bevestigd.
        """
        logger.info(f"Stap 4: Bevestigen (dry_run={dry_run})")

        self.page.wait_for_timeout(1000)
        self._screenshot("08_stap4_bevestigen")

        if dry_run:
            logger.info("DRY RUN - Reservering wordt NIET bevestigd")
            return True

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
                    logger.info(f"Bevestigingsknop gevonden: {selector}")
                    button.click()
                    self.page.wait_for_timeout(3000)
                    self._screenshot("08b_na_bevestiging")
                    return self._check_bevestiging()

            logger.error("Geen bevestigingsknop gevonden")
            self._screenshot("08_geen_bevestigingsknop")
            return False

        except Exception as e:
            logger.error(f"Fout bij bevestiging: {e}")
            self._screenshot("08_bevestiging_fout")
            return False

    # =========================================================================
    # HULP-FUNCTIES
    # =========================================================================

    def _klik_volgende(self, stap_naam: str) -> bool:
        """Klik op de 'Volgende >' knop om naar de volgende stap te gaan."""
        logger.info(f"Klik op 'Volgende' ({stap_naam})")
        try:
            volgende_btn = self.page.locator(
                "button:has-text('Volgende'), "
                "a:has-text('Volgende')"
            ).first

            if volgende_btn.is_visible() and volgende_btn.is_enabled():
                volgende_btn.click()
                self.page.wait_for_load_state("networkidle")
                self.page.wait_for_timeout(2000)
                self._screenshot(f"{stap_naam}_na_volgende")
                logger.info(f"Doorgegaan naar volgende stap ({stap_naam})")
                return True
            else:
                logger.warning(f"'Volgende' knop niet zichtbaar of niet klikbaar ({stap_naam})")
                self._screenshot(f"{stap_naam}_volgende_niet_gevonden")
                return False
        except Exception as e:
            logger.error(f"Fout bij klikken op Volgende ({stap_naam}): {e}")
            return False

    def _check_bevestiging(self) -> bool:
        """Controleer of de reservering succesvol is bevestigd."""
        page_text = (self.page.text_content("body") or "").lower()

        success_indicators = [
            "reservering geplaatst", "reservering bevestigd",
            "succesvol gereserveerd", "baan gereserveerd",
            "gelukt", "bevestigd",
        ]
        error_indicators = [
            "fout", "error", "mislukt", "niet beschikbaar",
            "bezet", "al gereserveerd", "niet mogelijk",
        ]

        for indicator in success_indicators:
            if indicator in page_text:
                logger.info(f"Bevestiging gevonden: '{indicator}'")
                return True

        for indicator in error_indicators:
            if indicator in page_text:
                logger.warning(f"Foutmelding gevonden: '{indicator}'")
                return False

        logger.info("Geen duidelijke bevestiging gevonden - controleer screenshots")
        return True

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
        Bereid de reservering voor: login, stap 1 (spelers), stap 2 (dag).

        Dit wordt VOOR de 48-uur grens uitgevoerd zodat we klaarstaan
        op stap 3 wanneer de reservering opengaat.

        Args:
            target_date: Datum waarvoor gereserveerd moet worden.
            tijden: Lijst met voorkeurtijden (om dagdeel te bepalen).
            spelers: Lijst met medespelers.

        Returns:
            None bij succes, foutmelding-string bij falen.
        """
        try:
            if not self.login():
                return "Login mislukt"

            if not self.navigeer_naar_reservering():
                return "Kon niet naar reserveringspagina navigeren"

            if not self.stap1_partners_kiezen(spelers):
                return "Kon medespelers niet toevoegen (stap 1)"

            if not self.stap2_kies_dag(target_date, tijden=tijden):
                return f"Kon datum {target_date.strftime('%d-%m-%Y')} niet selecteren (stap 2)"

            logger.info("Voorbereiding klaar - klaarstaan op stap 3 (kies een baan)")
            return None

        except ReserveringError as e:
            return str(e)
        except Exception as e:
            return f"Onverwachte fout bij voorbereiding: {e}"

    def probeer_reserveer(
        self,
        tijden: list[str],
        baan_voorkeur: list = None,
        dry_run: bool = False,
    ) -> dict:
        """
        Probeer stap 3 (kies een baan) + stap 4 (bevestigen) uit te voeren.

        Dit is de methode die in de retry-loop wordt aangeroepen.

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
            # Herlaad de pagina om actuele beschikbaarheid te zien
            self.page.reload(wait_until="networkidle")
            self.page.wait_for_timeout(1500)

            slot = self.stap3_kies_baan(tijden, baan_voorkeur)
            if not slot:
                # Geen slot gevonden - kan betekenen dat het nog niet open is
                # of dat alles bezet is. Retry.
                result["foutmelding"] = "Geen beschikbaar tijdslot gevonden"
                result["retry"] = True
                return result

            result["tijd"] = slot["tijd"]
            result["baan"] = slot["baan"]

            if self.stap4_bevestigen(dry_run=dry_run):
                result["success"] = True
                if dry_run:
                    result["foutmelding"] = "DRY RUN - niet daadwerkelijk gereserveerd"
            else:
                # Bevestiging mislukt - mogelijk bezet door iemand anders
                result["foutmelding"] = "Bevestiging mislukt (mogelijk al bezet)"
                result["retry"] = True

        except Exception as e:
            result["foutmelding"] = f"Fout: {e}"
            result["retry"] = True
            logger.error(f"Fout bij reserveerpoging: {e}")

        self._screenshot("poging_resultaat")
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

            poging = self.probeer_reserveer(tijden, baan_voorkeur, dry_run)
            result["success"] = poging["success"]
            result["tijd"] = poging["tijd"]
            result["baan"] = poging["baan"]
            result["foutmelding"] = poging["foutmelding"]

        except Exception as e:
            result["foutmelding"] = f"Onverwachte fout: {e}"
            logger.error(f"Onverwachte fout: {e}", exc_info=True)
        finally:
            self._screenshot("09_eindresultaat")

        return result

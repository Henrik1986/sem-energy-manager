
from homeassistant import config_entries
import voluptuous as vol
import os
import aiohttp
import zipfile
import shutil

DOMAIN = "sem"

ZIP_URL = "https://github.com/Henrik1986/huawei-energy-managment/archive/refs/heads/main.zip"


class SEMConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):

    VERSION = 1

    def __init__(self):
        self.nordpool_verified = False
        self.hacs_verified = False
        self.smhi_verified = False
        self.ai_done = False
        self.yaml_done = False
        

    # ─────────────────────────────
    # START
    # ─────────────────────────────
    async def async_step_user(self, user_input=None):

        if user_input is not None:
            return await self.async_step_nordpool()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({}),
        )

    # ─────────────────────────────
    # NORDPOOL
    # ─────────────────────────────
    async def async_step_nordpool(self, user_input=None):

        def nordpool_exists():
            return any(
                entity_id.startswith("sensor.nordpool")
                for entity_id in self.hass.states.async_entity_ids("sensor")
            )

        nordpool_ok = nordpool_exists()

        status = (
            "🟡 Nordpool integration hittad.\n\n" 
            "Klicka på bekräfta för att validera din sensor."
            if nordpool_ok
            else (
                "🔴 Hittar ingen Nordpool integration\n\n\n"
                "1. Hämta Nordpool via HACS och starta om Home Assistant.\n" 
                "2. Installera och konfiguera Nordpool integrationen i Home Assistant. Din sensor ska visa elpriset i SEK/kWh.\n"
                "(Inställningar > Enheter & tjänster > Lägg till integration)\n"
                "3. Starta om SEM-installationen för att fortsätta installationen.\n\n\n"
                "OBS! Bocka i rutan nedan om du använder den officiella Nordpool integrationen från Home Assistant.\n"
                "https://www.home-assistant.io/integrations/nordpool/"
            )
        )

        if user_input is not None:

            if user_input.get("official_nordpool_installed"):
                return await self.async_step_hacs()

            if nordpool_ok:

                if self.nordpool_verified:
                    return await self.async_step_hacs()

                self.nordpool_verified = True

                return self.async_show_form(
                    step_id="nordpool",
                    data_schema=vol.Schema({}),
                    description_placeholders={
                        "status": (
                            "🟢 Nordpool sensor är validerad och godkänd.\n\n"
                            "Klicka på bekräfta för att fortsätta installationen."
                        )
                    }
                )

            return self.async_show_form(
                step_id="nordpool",
                data_schema=vol.Schema({}),
                description_placeholders={"status": status}
            )

        return self.async_show_form(
            step_id="nordpool",
            data_schema=vol.Schema({
                vol.Optional(
                    "official_nordpool_installed",
                    default=False
                ): bool
            }),
            description_placeholders={"status": status}
        )

    # ─────────────────────────────
    # HACS
    # ─────────────────────────────
    async def async_step_hacs(self, user_input=None):

        def check(path):
            return os.path.isdir(path)

        apex_ok = check("/config/www/community/apexcharts-card")
        layout_ok = check("/config/www/community/lovelace-layout-card")
        vertical_ok = check("/config/www/community/vertical-stack-in-card")
        energy_ok = check("/config/www/community/energy-flow-card-plus")
        bubble_ok = check("/config/www/community/Bubble-Card")

        missing = []

        if not apex_ok:
            missing.append("• ApexCharts Card")

        if not layout_ok:
            missing.append("• layout-card")

        if not vertical_ok:
            missing.append("• Vertical Stack In Card")

        if not energy_ok:
            missing.append("• Energy Flow Card Plus")

        if not bubble_ok:
            missing.append("• Bubble Card")

        all_ok = len(missing) == 0

        status = (
            "🟡 Nödvändiga HACS-integrationer hittade.\n\n" 
            "Klicka på bekräfta för att validera dina integrationer."
            if all_ok
            else (
                "🔴 Följande HACS-integrationer saknas:\n\n"
                + "\n".join(missing)
                + "\n\nInstallera dessa via HACS innan du klickar bekräfta."
            )
        )

        if user_input is not None:

            if all_ok:

                if self.hacs_verified:
                    return await self.async_step_smhi()

                self.hacs_verified = True

                return self.async_show_form(
                    step_id="hacs",
                    data_schema=vol.Schema({}),
                    description_placeholders={
                        "status": (
                            "🟢 HACS-integrationerna är validerade och godkända.\n\n"
                            "Klicka på bekräfta för att fortsätta installationen."
                        )
                    }
                )

            return self.async_show_form(
                step_id="hacs",
                data_schema=vol.Schema({}),
                description_placeholders={"status": status}
            )

        return self.async_show_form(
            step_id="hacs",
            data_schema=vol.Schema({}),
            description_placeholders={"status": status}
        )

    # ─────────────────────────────
    # SMHI
    # ─────────────────────────────
    async def async_step_smhi(self, user_input=None):

        def smhi_exists():
            return self.hass.states.get("weather.smhi_home") is not None

        smhi_ok = smhi_exists()

        status = (
            "🟡 SMHI-integrationen och vädersensor hittades.\n\n" 
            "Klicka på bekräfta för att validera integrationen och vädersensorn."
            if smhi_ok
            else (
                "🔴 SMHI-integrationen är inte tillgänglig eller inte korrekt konfigurerad.\n\n"
                "- Inställningar > Enheter och tjänster > Lägg till integration \n"  
                "- Hitta och klicka på SMHI > Konfigurera > Enhetsnamn: Home"
            )
        )

        if user_input is not None:

            if smhi_ok:

                if self.smhi_verified:
                    return await self.async_step_ai()

                self.smhi_verified = True

                return self.async_show_form(
                    step_id="smhi",
                    data_schema=vol.Schema({}),
                    description_placeholders={
                        "status": (
                            "🟢 SMHI-integrationen och vädersensorn är validerad och godkänd.\n\n"
                            "Klicka på bekräfta för att fortsätta installationen."
                        )
                    }
                )

            return self.async_show_form(
                step_id="smhi",
                data_schema=vol.Schema({}),
                description_placeholders={"status": status}
            )

        return self.async_show_form(
            step_id="smhi",
            data_schema=vol.Schema({}),
            description_placeholders={"status": status}
        )

    # ─────────────────────────────
    # AI
    # ─────────────────────────────
    async def async_step_ai(self, user_input=None):

        if user_input is not None:
            return await self.async_step_yaml_setup()

        return self.async_show_form(
            step_id="ai",
            data_schema=vol.Schema({}),
            description_placeholders={
                "status": (
                    "🤖✨ AI-agent\n\n"
                    "Systemet fungerar även utan AI, men med AI-analys kan styrningen bli mer träffsäker och tydlig. Du kan alltid lägga till en AI-agent senare i systemet.\n\n"
                    "- Google AI (gratis men ostabilare)\n" 
                    "https://www.home-assistant.io/integrations/google_generative_ai_conversation/\n\n"
                    "- OpenAI (kostar men stabilare) \n"
                    "https://www.home-assistant.io/integrations/openai_conversation/"
                )
            }
        )

    # ─────────────────────────────
    # YAML
    # ─────────────────────────────
    async def async_step_yaml_setup(self, user_input=None):

        if user_input is not None:
            return await self.async_step_install()

        yaml_text = (
            "Steg 1 - Installera File Editor 📝\n\n"
            "- Inställningar > Appar > Installera app\n"
            "- Hitta och klicka på File Editor\n"
            "- Klicka på installera\n"
            "- Starta File Editor och öppna sen webbgränssnitt\n\n\n\n"
  
            "Steg 2 - Hitta och öppna din configuration.yaml 🔍\n\n"
            "- Klicka på 'mappen' uppe till vänster i File Editor\n"
            "- Leta upp och klicka på filen configuration.yaml\n\n\n\n"
  
            "Steg 3 - Kopiera in nedanstående kod <b><u>överst</u></b> i din configuration.yaml ✂️\n\n"
            "```yaml\n"
            "homeassistant:\n"
            "  packages: !include_dir_named packages\n"
            "```\n\n\n\n"

            "Steg 4 - Kopiera in nedanstående kod <b><u>längst ner</u></b> i din configuration.yaml och klicka på spara✂️\n\n"
            "```yaml\n"
            "lovelace:\n"
            "  mode: storage\n"
            "  dashboards:\n"
            "    smart-energy-system:\n"
            "      mode: yaml\n"
            "      title: Energisystem\n"
            "      icon: mdi:dots-circle\n"
            "      show_in_sidebar: true\n"
            "      filename: dashboards/admin_view.yaml\n"
            "```\n\n\n\n"

            "Steg 5 - Kontrollera din installation 🧐\n\n"
            "- Inställningar > Utvecklarverktyg > Klicka på kontrollera konfiguration\n"
            "- Klicka på bekräfta om du fått en bekräftelse att Home Assistant inte kommer att hindras från att starta! ✅"
        )

        return self.async_show_form(
            step_id="yaml_setup",
            data_schema=vol.Schema({}),
            description_placeholders={"status": yaml_text}
        )

    # ─────────────────────────────
    # INSTALLATION SEM
    # ─────────────────────────────
    async def async_step_install(self, user_input=None):

        if user_input is not None:

            base = self.hass.config.path()
            tmp_zip = os.path.join(base, "sem_tmp.zip")
            tmp_dir = os.path.join(base, "sem_tmp")

            packages_dir = self.hass.config.path("packages")
            dashboards_dir = self.hass.config.path("dashboards")

            os.makedirs(packages_dir, exist_ok=True)
            os.makedirs(dashboards_dir, exist_ok=True)

            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(ZIP_URL) as resp:
                        resp.raise_for_status()
                        data = await resp.read()

                with open(tmp_zip, "wb") as f:
                    f.write(data)

                shutil.rmtree(tmp_dir, ignore_errors=True)

                with zipfile.ZipFile(tmp_zip, "r") as z:
                    z.extractall(tmp_dir)

                base_extracted = os.path.join(tmp_dir, "huawei-energy-managment-main")

                source_backend = os.path.join(base_extracted, "sem")
                target_backend = os.path.join(packages_dir, "sem")

                if os.path.exists(target_backend):
                    shutil.rmtree(target_backend)

                shutil.copytree(source_backend, target_backend)

                source_frontend = os.path.join(base_extracted, "dashboards")

                if os.path.exists(source_frontend):
                    for item in os.listdir(source_frontend):
                        s = os.path.join(source_frontend, item)
                        d = os.path.join(dashboards_dir, item)

                        if os.path.isdir(s):
                            if os.path.exists(d):
                                shutil.rmtree(d)
                            shutil.copytree(s, d)
                        else:
                            shutil.copy2(s, d)

                os.remove(tmp_zip)
                shutil.rmtree(tmp_dir, ignore_errors=True)

            except Exception as e:
                raise

            return await self.async_step_finish()

        return self.async_show_form(
            step_id="install",
            data_schema=vol.Schema({}),
            description_placeholders={
                "status": (
                    "⚙️ Vi är redo att installera systemet\n\n"
                    "I detta steg kommer installationen att:\n"
                    "- Ladda ner filer till din enhet\n"
                    "- Installera systemets logik och automatik\n"
                    "- Installera kontrollpanelen\n\n"
                    "Klicka på bekräfta för att starta installationen av systemet."
                )
            }
        )

    # ─────────────────────────────
    # KLAR
    # ─────────────────────────────
    async def async_step_finish(self, user_input=None):

        if user_input is not None:

            entry = self.async_create_entry(
                title="SEM",
                data={"installed": True}
            )

            self.hass.async_create_task(
                self.hass.services.async_call(
                    "homeassistant",
                    "restart",
                    {},
                    blocking=False
                )
            )

            return entry

        finish_text = (
            "🎉 Installationen är klar!\n\n"
            "Systenet är nu installerat och redo att konfigureras efter en omstart av Home Assistant.\n\n"
            "För att komma igång:\n"
            "1. Öppna dashboarden 'Energisystem' i menyn till vänster.\n"
            "2. Klicka på Mitt system högst upp på sidan.\n"
            "3. Klicka på Konfigurera och följ anvisningarna.\n\n"
            "────────────────────────────\n\n"
            "🔄 Uppdateringar\n\n"
            "Framtida uppdateringar av systemet hanteras direkt från dashboarden via ikonen Mitt system.\n"
            "────────────────────────────\n\n"
            "🔑 Användarkod\n\n"
            "Vissa mer avancerade funktioner kräver en användakod. Läs mer om dessa där du ansöker om en användarkod.\n"
            "Användarkod ansöker du via Mitt system > Köp användarkod.\n"
            "Alla nya användare får 30 dagars provperiod.\n\n\n"
            "⚠️ När du klickar på bekräfta startas Home Assistant om automatiskt."
        )

        return self.async_show_form(
            step_id="finish",
            data_schema=vol.Schema({}),
            description_placeholders={"status": finish_text}
        )


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
            "✔ Nordpool hittad."
            if nordpool_ok
            else "❌ Installera Nordpool + skapa sensor innan du fortsätter."
        )

        if user_input is not None:

            if nordpool_ok:

                if self.nordpool_verified:
                    return await self.async_step_hacs()

                self.nordpool_verified = True

                return self.async_show_form(
                    step_id="nordpool",
                    data_schema=vol.Schema({}),
                    description_placeholders={"status": "✔ Nordpool OK. Klicka igen."}
                )

            return self.async_show_form(
                step_id="nordpool",
                data_schema=vol.Schema({}),
                description_placeholders={"status": status}
            )

        return self.async_show_form(
            step_id="nordpool",
            data_schema=vol.Schema({}),
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

        all_ok = apex_ok and layout_ok and vertical_ok and energy_ok and bubble_ok

        status = "✔ HACS OK." if all_ok else "❌ Saknade HACS-komponenter"

        if user_input is not None:

            if all_ok:

                if self.hacs_verified:
                    return await self.async_step_smhi()

                self.hacs_verified = True

                return self.async_show_form(
                    step_id="hacs",
                    data_schema=vol.Schema({}),
                    description_placeholders={"status": "✔ HACS OK. Klicka igen."}
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

        status = "✔ SMHI OK." if smhi_ok else "❌ weather.smhi_home saknas"

        if user_input is not None:

            if smhi_ok:

                if self.smhi_verified:
                    return await self.async_step_ai()

                self.smhi_verified = True

                return self.async_show_form(
                    step_id="smhi",
                    data_schema=vol.Schema({}),
                    description_placeholders={"status": "✔ SMHI OK. Klicka igen."}
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
                    "Valfritt steg.\n\n"
                    "- Google AI (gratis)\n"
                    "- OpenAI (kostnad)\n"
                    "Systemet fungerar utan detta steg."
                )
            }
        )

    # ─────────────────────────────
    # YAML STEP
    # ─────────────────────────────
    async def async_step_yaml_setup(self, user_input=None):

        if user_input is not None:
            return await self.async_step_install()

        yaml_text = (
            "Steg 1: Installera File Editor 📝\n\n"
            "Inställningar (kugghjulet ner till vänster) → Appar → Installera app (nere till höger) → Sök (högst upp) → File editor\n"
            "→ Klicka Installera → Klicka Start → Öppna webbgränssnitt\n\n"
            "────────────────────────────\n\n"
  
            "Steg 2: Hitta och öppna din configuration.yaml 🔍\n\n"
            "I File editor → Klicka mappen uppe till vänster → klicka på configuration.yaml\n\n"
            "────────────────────────────\n\n"
  
            "Steg 3: Kopiera in nedanstående kod överst i din configuration.yaml ✂️\n\n"
            "```yaml\n"
            "homeassistant:\n"
            "  packages: !include_dir_named packages\n"
            "```\n\n"
            "────────────────────────────\n\n"

            "Steg 4: Kopiera in nedanstånede kod längst ner i din configuration.yaml ✂️\n\n"
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
            "```\n\n"
            "────────────────────────────\n\n"

            "Steg 5: Kontrollera din installation 🧐\n\n"
            "Inställningar (kugghjulet nere till vänster) → Utvecklarverktyg → Klicka på kontrollera konfiguration\n"
            "Fortsätt endast installationen om en grön bock visas ✅"
        )

        return self.async_show_form(
            step_id="yaml_setup",
            data_schema=vol.Schema({}),
            description_placeholders={"status": yaml_text}
        )

    # ─────────────────────────────
    # INSTALLATION (NYTT – INBYGGT I WIZARD)
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
                    "Redo att installera SEM.\n\n"
                    "Detta kommer:\n"
                    "- Ladda ner paket\n"
                    "- Installera backend\n"
                    "- Installera dashboards\n"
                )
            }
        )

    # ─────────────────────────────
    # KLAR
    # ─────────────────────────────
    async def async_step_finish(self, user_input=None):

        if user_input is not None:
            return self.async_create_entry(
                title="SEM",
                data={"installed": True}
            )

        finish_text = (
            "🎉 Installationen är klar!\n\n"
            "SEM är nu installerat och redo att konfigureras.\n\n"
            "För att komma igång:\n"
            "1. Öppna dashboarden 'Energisystem' i menyn till vänster.\n"
            "2. Klicka på 'Mitt system' högst upp på sidan.\n"
            "3. Klicka på 'Konfigurera' och följ anvisningarna.\n\n"
            "────────────────────────────\n\n"
            "🔄 Uppdateringar\n\n"
            "Framtida uppdateringar av SEM hanteras direkt från dashboarden.\n"
            "Öppna 'Mitt system' och klicka på uppdateringsknappen.\n"
            "När uppdateringen är klar startar du om Home Assistant.\n\n"
            "────────────────────────────\n\n"
            "🔑 Licens och demoperiod\n\n"
            "Licens ansöks via 'Mitt system'.\n"
            "Alla nya användare får 30 dagars demo."
        )

        return self.async_show_form(
            step_id="finish",
            data_schema=vol.Schema({}),
            description_placeholders={"status": finish_text}
        )

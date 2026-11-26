from homeassistant import config_entries
import voluptuous as vol
import os

DOMAIN = "sem"


MARKETPLACE_CARDS = {
    "ApexCharts Card": {
        "directory": "apexcharts-card",
        "url": "https://my.home-assistant.io/redirect/marketplace_repository/?owner=RomRider&repository=apexcharts-card",
    },
    "Vertical Stack In Card": {
        "directory": "vertical-stack-in-card",
        "url": "https://my.home-assistant.io/redirect/marketplace_repository/?owner=ofekashery&repository=vertical-stack-in-card",
    },
    "Energy Flow Card Plus": {
        "directory": "energy-flow-card-plus",
        "url": "https://my.home-assistant.io/redirect/marketplace_repository/?owner=flixlix&repository=energy-flow-card-plus",
    },
    "Bubble Card": {
        "directory": "Bubble-Card",
        "url": "https://my.home-assistant.io/redirect/marketplace_repository/?owner=Clooos&repository=Bubble-Card",
    },
}


def find_missing_marketplace_cards(paths):
    return [name for name, path in paths.items() if not os.path.isdir(path)]


class SEMConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):

    VERSION = 1

    def __init__(self):
        self.nordpool_verified = False
        self.marketplace_verified = False
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
                "1. Hämta Nordpool via Marketplace (HACS) och starta om Home Assistant.\n" 
                "2. Installera och konfiguera Nordpool integrationen i Home Assistant. Din sensor ska visa elpriset i SEK/kWh.\n"
                "(Inställningar > Enheter & tjänster > Lägg till integration)\n"
                "3. Starta om SEM-installationen för att fortsätta installationen.\n\n\n"
                "OBS! Bocka i rutan nedan om du använder den officiella Nordpool integrationen från Home Assistant.\n"
                "https://www.home-assistant.io/integrations/nordpool/"
            )
        )

        if user_input is not None:

            if user_input.get("official_nordpool_installed"):
                return await self.async_step_marketplace()

            if nordpool_ok:

                if self.nordpool_verified:
                    return await self.async_step_marketplace()

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
    # MARKETPLACE
    # ─────────────────────────────
    async def async_step_marketplace(self, user_input=None):

        marketplace_paths = {
            name: self.hass.config.path("www", "community", card["directory"])
            for name, card in MARKETPLACE_CARDS.items()
        }
        missing = await self.hass.async_add_executor_job(
            find_missing_marketplace_cards, marketplace_paths
        )

        all_ok = not missing

        status = (
            "🟡 Alla nödvändiga frontend-kort hittades i Marketplace.\n\n"
            "Klicka på bekräfta för att validera installationerna."
            if all_ok
            else (
                "🔴 Följande frontend-kort saknas i Marketplace:\n\n"
                + "\n".join(
                    f"• {name} – [Klicka här för att installera]({MARKETPLACE_CARDS[name]['url']})"
                    for name in missing
                )
                + "\n\nÖppna länkarna för att installera korten via Marketplace. "
                "Gå sedan tillbaka till SEM och klicka på bekräfta för att kontrollera igen."
            )
        )

        if user_input is not None:

            if all_ok:

                if self.marketplace_verified:
                    return await self.async_step_smhi()

                self.marketplace_verified = True

                return self.async_show_form(
                    step_id="marketplace",
                    data_schema=vol.Schema({}),
                    description_placeholders={
                        "status": (
                            "🟢 Alla nödvändiga frontend-kort är installerade och validerade.\n\n"
                            "Klicka på bekräfta för att fortsätta installationen."
                        )
                    }
                )

            return self.async_show_form(
                step_id="marketplace",
                data_schema=vol.Schema({}),
                description_placeholders={"status": status}
            )

        return self.async_show_form(
            step_id="marketplace",
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
            return await self.async_step_finish()

        yaml_text = (
            "HACS har installerat SEM:s YAML-paket, skript, datafiler och dashboard under /config/custom_components/sem/resources/. "
            "Nedan aktiverar du filerna direkt från den platsen. Inga filer kopieras till /config/packages eller /config/dashboards.\n\n"
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
            "  packages: !include_dir_named custom_components/sem/resources/packages/sem\n"
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
            "      filename: custom_components/sem/resources/dashboards/admin_view.yaml\n"
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
            "Uppdateringar av SEM hanteras via HACS. Starta om Home Assistant efter en uppdatering för att läsa in de nya YAML-filerna.\n"
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

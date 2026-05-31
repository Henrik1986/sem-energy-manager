from homeassistant import config_entries
import voluptuous as vol

DOMAIN = "sem"


class SEMConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):

    VERSION = 1

    async def async_step_user(self, user_input=None):

        return await self.async_step_smhi()


    async def async_step_smhi(self, user_input=None):

        smhi_loaded = self.hass.states.get("weather.smhi_home") is not None

        if user_input is not None:
            return self.async_create_entry(
                title="SEM",
                data={}
            )

        smhi_status = (
            "✔ SMHI konfigurerad"
            if smhi_loaded
            else "❌ SMHI saknas (skapa weather.smhi_home först)"
        )

        return self.async_show_form(
            step_id="smhi",
            data_schema=vol.Schema({}),
            description_placeholders={
                "status": smhi_status
            }
        )

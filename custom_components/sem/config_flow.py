from homeassistant import config_entries
import voluptuous as vol

DOMAIN = "sem"


class SEMConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):

    VERSION = 1

    async def async_step_user(self, user_input=None):

        if user_input is not None:
            return await self.async_step_smhi()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({}),
        )

    async def async_step_smhi(self, user_input=None):

        smhi_loaded = self.hass.states.get("weather.smhi_home") is not None

        status = (
            "✔ SMHI konfigurerad"
            if smhi_loaded
            else "❌ SMHI saknas"
        )

        # Refresh (klick på submit = reload step)
        if user_input is not None:
            return await self.async_step_smhi()

        return self.async_show_form(
            step_id="smhi",
            data_schema=vol.Schema({
                vol.Optional("refresh"): bool
            }),
            description_placeholders={
                "status": status
            }
        )

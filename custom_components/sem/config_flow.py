from homeassistant import config_entries
import voluptuous as vol

DOMAIN = "sem"


class SEMConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):

    VERSION = 1

    async def async_step_user(self, user_input=None):

        return await self.async_step_requirements_check()


    async def async_step_requirements_check(self, user_input=None):

        smhi_loaded = "sensor.smhi_weather" in self.hass.states

        if user_input is not None:
            return self.async_create_entry(
                title="SEM",
                data={}
            )

        smhi_status = "✔ SMHI installerad" if smhi_loaded else "❌ SMHI saknas"

        return self.async_show_form(
            step_id="requirements_check",
            data_schema=vol.Schema({}),
            description_placeholders={
                "status": smhi_status
            }
        )

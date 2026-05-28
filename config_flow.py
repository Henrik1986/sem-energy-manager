from homeassistant import config_entries
from .const import DOMAIN
from .detectors.huawei import check_huawei_setup

class SEMConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):

```
VERSION = 1

async def async_step_user(self, user_input=None):

    status = await check_huawei_setup(self.hass)

    # Huawei Solar saknas helt
    if not status["installed"]:

        return self.async_show_form(
            step_id="user",
            errors={
                "base": "huawei_not_installed"
            }
        )

    # Huawei Solar är installerad men inte konfigurerad
    if not status["configured"]:

        return self.async_show_form(
            step_id="user",
            errors={
                "base": "huawei_not_configured"
            }
        )

    # Inga devices hittades
    if not status["devices_found"]:

        return self.async_show_form(
            step_id="user",
            errors={
                "base": "no_devices_found"
            }
        )

    # Inga entities hittades
    if not status["entities_found"]:

        return self.async_show_form(
            step_id="user",
            errors={
                "base": "no_entities_found"
            }
        )

    # Batteri hittades inte
    if not status["battery_found"]:

        return self.async_show_form(
            step_id="user",
            errors={
                "base": "battery_not_found"
            }
        )

    # Allt OK -> gå vidare
    return await self.async_step_configure()

async def async_step_configure(self, user_input=None):

    if user_input is not None:

        return self.async_create_entry(
            title="SEM",
            data=user_input
        )

    return self.async_show_form(
        step_id="configure"
    )
```

# Smart Energy Manager (SEM)
Ett kraftfullt HACS-paket för Home Assistant som optimerar din energianläggning med solceller och batteri – helt automatiskt. Systemet analyserar elpriser, batteriets status och solprognoser i realtid för att maximera egenanvändning och lönsamhet, utan manuell styrning.

<img width="1573" height="877" alt="Skärmbild 2026-06-16 211824" src="https://github.com/user-attachments/assets/9c6cc339-61f4-4ff6-99d1-4d24b8e47e08" />

## Funktioner

- Optimerar laddning och urladdning baserat på elpris, solprognos och behov
- Säljer överskottsel när det är som mest lönsamt
- Laddar batteriet när elen är billig – med hänsyn till batteriets livslängd
- Begränsar export vid negativa elpriser
- Förhindrar att elbilen laddas från batteriet
- Stöd för AI-analys som förbättrar styrning och beslut över tid

Designat för att ge högre självförsörjning, bättre ekonomi och full kontroll över din energianläggning.

## Viktigt innan du börjar installationen
Din anläggning måste vara integrerad med Home Assistant. 

Systemet har stöd för
- Huawei – https://github.com/wlcrs/huawei_solar
- Solax - https://github.com/wills106/homeassistant-solax-modbus [BETA]

### Huawei – kontrollera före installation av SEM

Om du använder Huawei Solar ska sensorn **Rated Capacity** vara aktiverad i Home Assistant innan SEM installeras.

Gör så här för varje batterienhet:
1. Öppna **Inställningar → Enheter och tjänster → Huawei Solar**.
2. Öppna batterienheten som heter **Batteries** eller **Connected energy storage**.
3. Aktivera entiteten **Rated Capacity**.
4. Kontrollera att sensorn visar batteriets nominella kapacitet i Wh.

SEM använder denna kapacitet för att beräkna en korrekt kapacitetsviktad SOC när flera batterier finns. SEM aktiverar inte sensorn automatiskt. Om den saknas eller är inaktiverad visas ett installationsfel i SEM:s entitetsstatus.

### Flera Huawei-växelriktare

SEM kan samla flera Huawei-växelriktare och batterier till en gemensam anläggning, både när de är kopplade via master/slav och när de är inlagda som separata Huawei Solar-anslutningar.

I den här versionen behandlas samtliga aktiva Huawei Solar-anslutningar som en och samma SEM-anläggning. Använd därför inte denna funktion för två fristående Huawei-anläggningar.

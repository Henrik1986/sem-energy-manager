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

Anläggningar med fullt stöd
- Huawei – https://github.com/wlcrs/huawei_solar ([Läs mer här](huawei.md)).

Nedanstående anläggningar är kopplade, men ännu inte fullt testade. 
- Solax - https://github.com/wills106/homeassistant-solax-modbus
- Solis - https://github.com/wills106/homeassistant-solax-modbus

## Installation

1. Installera **Smart Energy Manager (SEM)** via HACS.
2. Starta om Home Assistant och lägg till SEM under **Inställningar → Enheter och tjänster → Lägg till integration**.
3. Följ installationsflödet. Det kontrollerar att Nordpool, nödvändiga frontend-kort och SMHI finns tillgängliga och visar därefter YAML-konfigurationen som ska aktiveras.
4. HACS installerar integrationen under `/config/custom_components/sem/`. SEM:s paketfiler, skript, datafiler och dashboard finns under `/config/custom_components/sem/resources/`.
5. Lägg till följande under `homeassistant:` i `configuration.yaml`:

```yaml
homeassistant:
  packages: !include_dir_named custom_components/sem/resources/packages/sem
```

Om ett `homeassistant:`-block redan finns ska du lägga till `packages:` under det befintliga blocket, inte skapa ett till.

6. Registrera SEM-dashboarden i `configuration.yaml`:

```yaml
lovelace:
  mode: storage
  dashboards:
    smart-energy-system:
      mode: yaml
      title: Energisystem
      icon: mdi:dots-circle
      show_in_sidebar: true
      filename: custom_components/sem/resources/dashboards/admin_view.yaml
```

7. Kontrollera Home Assistant-konfigurationen och starta om Home Assistant.

### Kom igång efter installationen

När Home Assistant har startat om:

1. Öppna dashboarden **Energisystem** via sidomenyn.
2. På dashboardens hemvy klickar du på ikonen **Mitt system** i den övre raden.
3. Klicka på **Konfigurera** och följ anvisningarna för att färdigställa systemets inställningar.

### Uppdateringar

Integrationskoden, YAML-paketen, skripten, datafilerna och dashboarden levereras tillsammans i varje SEM-release. HACS installerar de uppdaterade filerna när integrationen uppdateras. Starta om Home Assistant efter uppdateringen så att de nya YAML-filerna läses in.

SEM laddar inte längre ner eller uppdaterar paketfiler separat från dashboarden.

### ⚠️ Befintliga installationer

Om du uppdaterar från en tidigare SEM-version behöver du först ändra länkarna i `configuration.yaml` så att de gamla filerna inte längre läses in.

1. Ta bort eller ersätt den gamla paketsökvägen: `!include_dir_named packages`.
2. Ta bort eller ersätt den gamla dashboard-sökvägen: `filename: dashboards/admin_view.yaml`.
3. När de gamla sökvägarna är borttagna lägger du in de nya sökvägarna som visas i installationsanvisningarna ovan. De pekar på filernas nya plats `custom_components/sem/resources/`.

Det är viktigt att först ta bort de gamla länkarna och därefter aktivera de nya. Annars riskerar Home Assistant att läsa in både de gamla och nya paketfilerna parallellt. Säkerhetskopiera eventuella egna ändringar i de gamla filerna innan du tar bort eller arkiverar dem.

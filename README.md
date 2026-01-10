# Nextcloud Talk Bot for Claude Code

Een Flask-gebaseerde bot die Nextcloud Talk koppelt aan Claude Code, waardoor AI-gesprekken direct in Nextcloud Talk chats mogelijk zijn.

## Features

- **Multi-user support**: Elke gebruiker heeft zijn eigen configuratie en bot-secret
- **Conversatie geschiedenis**: Houdt context per chat bij
- **WhisperFlow integratie**: Audio transcriptie ondersteuning
- **ERPNext integratie**: Koppeling met ERPNext via MCP servers
- **Nextcloud Deck**: Taakbeheer integratie met Deck kaarten
- **Natuurlijke taal**: Detecteert wanneer taken afgerond zijn

## Vereisten

- Ubuntu Server 20.04 of nieuwer
- Docker en Docker Compose
- Nextcloud met Talk app
- Anthropic API key voor Claude
- (Optioneel) ERPNext instance

## Installatie

### Stap 1: Clone de repository

```bash
sudo mkdir -p /opt/nextcloud-claude-bot
cd /opt/nextcloud-claude-bot
sudo git clone https://github.com/OpenAEC-Foundation/nextcloud-talk-erp-next-bot.git .
```

### Stap 2: Maak de data directory

```bash
sudo mkdir -p data
```

### Stap 3: Configureer environment variabelen

```bash
sudo cp .env.example .env
sudo nano .env
```

Vul de volgende waarden in:

| Variabele | Omschrijving | Voorbeeld |
|-----------|-------------|-----------|
| `NEXTCLOUD_URL` | URL van je Nextcloud instance | `https://cloud.example.nl` |
| `ERPNEXT_URL` | URL van je ERPNext instance (optioneel) | `https://erp.example.nl` |
| `ANTHROPIC_API_KEY` | Je Anthropic API key | `sk-ant-xxx...` |

### Stap 4: Configureer bot gebruikers

```bash
sudo cp bots_config.json.example data/bots_config.json
sudo nano data/bots_config.json
```

Voor elke gebruiker die de bot wil gebruiken, voeg een entry toe:

```json
{
    "gebruikersnaam": {
        "secret": "bot-secret-uit-nextcloud-talk",
        "working_dir": "/home/gebruiker",
        "config_dir": "/opt/nextcloud-claude-bot/data/config/gebruikersnaam",
        "erpnext_api_key": "je-erpnext-api-key",
        "erpnext_api_secret": "je-erpnext-api-secret",
        "erpnext_user": "gebruiker@example.nl",
        "nextcloud_user": "gebruikersnaam",
        "nextcloud_password": "nextcloud-app-wachtwoord"
    }
}
```

#### Waar vind je deze waarden?

| Waarde | Waar te vinden |
|--------|---------------|
| `secret` | Nextcloud → Settings → Talk → Bots → Voeg bot toe → Secret |
| `nextcloud_password` | Nextcloud → Settings → Security → App passwords → Genereer nieuw wachtwoord |
| `erpnext_api_key/secret` | ERPNext → User → API Access → Generate Keys |

### Stap 5: Configureer Nextcloud Talk Bot

1. Ga naar Nextcloud → Instellingen → Talk → Bots
2. Klik "Bot toevoegen"
3. Vul in:
   - **Naam**: Claude Bot (of andere naam)
   - **Callback URL**: `http://je-server-ip:8085/webhook/gebruikersnaam`
   - **Secret**: Kopieer het gegenereerde secret naar `bots_config.json`
4. Sla op

### Stap 6: Start de bot

```bash
cd /opt/nextcloud-claude-bot
sudo docker compose up -d
```

### Stap 7: Controleer of het werkt

```bash
# Bekijk logs
sudo docker compose logs -f

# Test de endpoint
curl http://localhost:8085/
```

## Configuratie bestanden

### `.env` - Globale configuratie

```bash
NEXTCLOUD_URL=https://cloud.example.nl      # Je Nextcloud URL
ERPNEXT_URL=https://erp.example.nl          # Je ERPNext URL (optioneel)
ANTHROPIC_API_KEY=sk-ant-xxx                # Anthropic API key
```

### `data/bots_config.json` - Per-gebruiker configuratie

Elke gebruiker heeft:
- `secret`: Het bot secret uit Nextcloud Talk instellingen
- `working_dir`: Map waar Claude commando's mag uitvoeren
- `config_dir`: Map voor Claude configuratie per gebruiker
- `erpnext_*`: ERPNext API credentials (optioneel)
- `nextcloud_*`: Nextcloud credentials voor file access

## Reverse Proxy (Nginx)

Als je Nginx gebruikt als reverse proxy:

```nginx
location /claude-bot/ {
    proxy_pass http://127.0.0.1:8085/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 180s;
}
```

Pas dan de Callback URL aan naar: `https://je-domein.nl/claude-bot/webhook/gebruikersnaam`

## Gebruik

1. Open Nextcloud Talk
2. Start een gesprek met de bot (of voeg de bot toe aan een groep)
3. Stuur een bericht - de bot antwoordt met Claude

### Speciale commando's

- Audio berichten worden automatisch getranscribeerd (indien WhisperFlow geconfigureerd)
- De bot onthoudt context binnen een conversatie
- Bij taak-gerelateerde chats kan de bot automatisch Deck kaarten updaten

## Problemen oplossen

### Bot reageert niet
```bash
# Check of container draait
sudo docker ps

# Bekijk logs
sudo docker compose logs --tail=100
```

### Webhook errors in Nextcloud
- Controleer of de Callback URL correct is
- Controleer of het secret overeenkomt in Nextcloud en `bots_config.json`
- Check of poort 8085 bereikbaar is (of via reverse proxy)

### Claude errors
- Controleer de `ANTHROPIC_API_KEY` in `.env`
- Controleer of de API key actief is op console.anthropic.com

## Updates

```bash
cd /opt/nextcloud-claude-bot
sudo git pull
sudo docker compose build
sudo docker compose up -d
```

## Licentie

MIT

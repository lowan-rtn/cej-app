# CEJ App AI Worker

Worker Cloudflare utilise par l'app CEJ pour transformer une demande utilisateur en commande JSON exploitable par `window.cejAgent`.

## Endpoints

- `GET /health`: verifie que le Worker repond.
- `POST /suggest`: genere une commande d'interface.

Exemple:

```bash
curl -s https://cej-app-ai.<subdomain>.workers.dev/suggest \
  -H 'Content-Type: application/json' \
  -d '{
    "message": "deplace l action CV au 2026-04-03",
    "state": {
      "weekStart": "2026-03-30",
      "weekEnd": "2026-04-05",
      "actions": [
        {
          "id": "action-id",
          "title": "Creation CV numerique",
          "comment": "",
          "date": "2026-04-02",
          "status": "done",
          "qualification": "EMPLOI"
        }
      ]
    }
  }'
```

## Developpement

```bash
cd worker
wrangler dev
```

## Deploiement

```bash
cd worker
wrangler deploy
```

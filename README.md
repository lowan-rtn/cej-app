# CEJ App

Application desktop Linux pour le suivi CEJ.

Contenu principal :
- `scripts/cej_web_ui.py` : interface locale
- `scripts/cej_desktop.py` : lanceur desktop `pywebview`
- `scripts/build_cej_deb.sh` : build du paquet Debian

Build `.deb` :

```bash
./scripts/build_cej_deb.sh 0.1.2
```

Lancement local :

```bash
./scripts/run_cej_desktop.sh
```

# pasarguardtelegrambot systemd service

Project path:

```bash
/home/PasarguardTelegramBot
```

Service name:

```bash
pasarguardtelegrambot
```

Install:

```bash
cd /home/PasarguardTelegramBot/service_files
sudo bash install_service.sh
```

Status:

```bash
systemctl status pasarguardtelegrambot
```

Logs:

```bash
journalctl -u pasarguardtelegrambot -f
```

Restart:

```bash
sudo systemctl restart pasarguardtelegrambot
```

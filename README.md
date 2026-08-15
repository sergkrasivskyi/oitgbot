# OI TG Bot (Binance Futures → Telegram)

Бот сканує **Binance USDⓈ-M Perpetual (USDT)** ф’ючерси, рахує зміну **Open Interest (OI)** та **ціни**, і публікує звіти в Telegram-канали за розкладом.

## Можливості

- **OI Binance HH** — “імпульси”: OI за 5 хвилин >= `IMPULSE_THRESHOLD`
- **OI Binance All** — топ по росту OI за 20 хвилин >= `TOP_THRESHOLD`
- Формат звіту: `OI% | PX% | Ticker` (тікери клікабельні → Coinglass)
- Фільтрація інструментів:
  - `PERPETUAL`
  - лише `...USDT`
  - лише ASCII (без ієрогліфів)
- Паралельне сканування (ThreadPool) для швидкості
- Кеш символів (TTL 1 година)
- Стійкість до нестабільного інтернету:
  - Binance timeout/retry
  - Telegram timeout + 1 retry
  - помилки відправки не валять scheduler

---

## Структура проєкту

```

.
├─ oitgbot/
│  ├─ app.py
│  ├─ config.py
│  ├─ logger_setup.py
│  ├─ models.py
│  ├─ scheduler_jobs.py
│  ├─ clients/
│  │  ├─ binance_api.py
│  │  └─ telegram_sender.py
│  └─ services/
│     ├─ oi_scanner.py
│     └─ report_formatter.py
├─ run.py
├─ requirements.txt
├─ .env
├─ Dockerfile
├─ docker-compose.yml
└─ .dockerignore

````

---

## Налаштування `.env`

Створи/онови файл `.env` в корені проєкту:

```env
BOT_TOKEN=your_telegram_bot_token
ALL_CHANNEL_ID=-1001234567890
PROP_CHANNEL_ID=-1001234567891

# Список "обраних" символів для другого каналу (опційно)
PROP_SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT

IMPULSE_THRESHOLD=5.0
TOP_THRESHOLD=1.0

# Порожні звіти (1 = надсилати, 0 = не надсилати)
SEND_EMPTY_REPORTS=0

# Якщо імпульсів нема, можна слати fallback TOP-N за OI_5m
SHOW_TOP_WHEN_EMPTY=0
TOP_WHEN_EMPTY_N=10

DEBUG_OI=0

LOG_FILE=bot.log
LOG_MAX_BYTES=5000000
LOG_BACKUP_COUNT=5

BINANCE_BASE_URL=https://fapi.binance.com
HTTP_TIMEOUT=5
HTTP_RETRIES=1

# Rolling OI shadow mode (diagnostics only; Telegram remains legacy-driven)
ROLLING_OI_SHADOW_ENABLED=1
ROLLING_OI_CADENCE_SECONDS=30
ROLLING_OI_WORKERS=20
ROLLING_OI_RETENTION_MINUTES=150
ROLLING_OI_PRICE_MAX_AGE_SECONDS=5
ROLLING_OI_OBSERVATION_MAX_AGE_SECONDS=60
ROLLING_OI_TRANSACTION_AGE_WARNING_SECONDS=60
ROLLING_OI_5M_OBSERVATION_PCT=2
ROLLING_OI_5M_TRIGGER_PCT=5
ROLLING_OI_5M_REARM_PCT=3
ROLLING_OI_20M_OBSERVATION_PCT=1
ROLLING_OI_60M_OBSERVATION_PCT=3
ROLLING_OI_120M_OBSERVATION_PCT=4
````

`ROLLING_OI_OBSERVATION_MAX_AGE_SECONDS` controls freshness of the bot's local
observation clock. The former `ROLLING_OI_MAX_OI_AGE_SECONDS` name is accepted
as a compatibility fallback but no longer rejects present OI because Binance's
transaction timestamp is old. `ROLLING_OI_TRANSACTION_AGE_WARNING_SECONDS`
controls diagnostics only.

The rolling 5m signal state machine uses quantity OI only. It triggers at
`ROLLING_OI_5M_TRIGGER_PCT`, re-arms at
`ROLLING_OI_5M_REARM_PCT`, and remains shadow-only: it logs transitions but
does not send Telegram messages. Its per-symbol state is in memory and starts
clean after an application restart.

### Пояснення ключових параметрів

* `IMPULSE_THRESHOLD` — поріг OI% за 5 хв (імпульси)
* `TOP_THRESHOLD` — поріг OI% за 20 хв (звіт All)
* `SEND_EMPTY_REPORTS=1` — надсилати повідомлення навіть якщо сигналів нема (з приміткою)
* `SHOW_TOP_WHEN_EMPTY=1` — якщо імпульсів нема, відправляти fallback TOP-N
* `HTTP_TIMEOUT/HTTP_RETRIES` — важливо для нестабільного інтернету

---

## Локальний запуск (без Docker)

### 1) Встановити залежності

**PowerShell (Windows):**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2) Запустити

```powershell
python run.py
```

---

## Docker (рекомендовано)

### Передумови

* Встановлений **Docker Desktop**
* Увімкнений автозапуск Docker Desktop:
  **Settings → General → Start Docker Desktop when you log in**

### Запуск у фоні

```powershell
docker compose up -d --build
```

### Логи

```powershell
docker compose logs -f
```

Вийти з перегляду логів: `Ctrl + C` (контейнер продовжує працювати)

### Перевірити статус

```powershell
docker ps
```

### Перезапуск

```powershell
docker restart oitgbot
```

або:

```powershell
docker compose restart
```

### Зупинити і прибрати контейнер

```powershell
docker compose down
```

### Після змін у коді (перебудувати образ)

```powershell
docker compose up -d --build
```

---

## Автовідновлення після ребуту / падіння

У `docker-compose.yml` використовується:

* `restart: unless-stopped`
* `stop_signal: SIGTERM`
* `stop_grace_period: 20s`

Це означає:

* після перезавантаження Windows і старту Docker Desktop контейнер підніметься сам
* при `docker stop` бот завершується коректно (graceful shutdown)

---

## Розклад (cron)

* **HH (impulses)**: кожні 5 хвилин `*/5` на `second=0`
* **All (top)**: `minute=0,20,40` на `second=10`

---

## Типові проблеми

### `Chat not found`

* неправильний `ALL_CHANNEL_ID` / `PROP_CHANNEL_ID`
* бот не доданий у канал або не має прав писати
* ID має бути у форматі `-100...`

### `Telegram send timeout`

Іноді Telegram приймає повідомлення, але відповідь приходить пізно → клієнт бачить `TimedOut`.
У нас є:

* збільшені таймаути в `Application.builder()`
* 1 повторна спроба в `TelegramSender`

### Нема інтернету

Контейнер не впаде. Можуть бути помилки у логах. Коли інтернет повернеться — бот продовжить роботу.

---

## Корисні команди (шпаргалка)

```powershell
# старт
docker compose up -d

# старт з перебудовою
docker compose up -d --build

# логи
docker compose logs -f

# статус
docker ps

# перезапуск
docker restart oitgbot

# стоп
docker compose down
```

---

## Безпека

* `.env` не додавай у git
* `BOT_TOKEN` тримай приватним

```
```

# vacancy-hunter

Локальный сбор удалённых вакансий с HeadHunter, публичных Telegram-каналов и RSS Хабр Карьеры. Поиск идёт обычными HTTP-запросами. На этапе отбора LLM не вызывается: решение принимает детерминированный фильтр по спискам слов и баллам.

## Архитектура

Корень репозитория — каталог с `hunter.py`. Вся предметная логика лежит в пакете `vacancy_hunter/`.

1. `hh_parser.py` делает `GET https://api.hh.ru/vacancies` по каждой фразе из `HH_QUERIES`. Параметры: `text`, `schedule=remote`, `order_by=publication_time`, `per_page=50`, `period=3`. Таймаут 10 секунд. Ответы `403` и `429` пишутся в лог, по этому запросу возвращается пустой список, остальные источники продолжают работу.
2. `tg_scraper.py` забирает HTML `https://t.me/s/<channel>` и через BeautifulSoup достаёт текст поста, дату и ссылку на сообщение. Короткие служебные блоки отбрасываются.
3. `habr_parser.py` скачивает RSS и разбирает его библиотекой `feedparser`.
4. `filter_and_score.py` смотрит название, описание и зарплату. Любое слово из `blacklist_hard` (границы слов, без учёта регистра) сразу отсекает вакансию: туда входят город, офис, гибрид и уровни senior / сеньор / lead / team lead / head / руководитель / главный / principal. Дальше нужны маркер вакансии и ядро `python` / `питон` / `пайтон`. Слово из whitelist повышает балл, но без него вакансия тоже проходит. Баллы: `+30` за каждое уникальное слово whitelist, `+10` если whitelist пуст, `+20` за junior / intern / «без опыта» / стажёра, `+15` за явную удалёнку, `+10` если публикация моложе 24 часов, `−15` за каждое уникальное совпадение `blacklist_soft` (`middle+`, стаж 3+ года или 5 лет, английский B2, English fluent). Ядро Python в эти `+30` не входит.
5. `database.py` хранит уже показанные вакансии и активные Telegram-каналы в SQLite. Если активных каналов нет, поиск берёт запасной список `TG_CHANNELS`. Ключ — SHA-256 от строки `источник + url`. Файл базы считается как `Path(__file__).resolve().parent.parent / "data" / "seen_vacancies.db"`. Каталог `data/` создаётся при первом запуске.
6. `vacancy_hunter/core.py` собирает три источника, фильтрует и записывает только новые строки. Дайджест `digests/digest_YYYY-MM-DD.md` и текст для Telegram строит один форматтер: разделы 🟢 HH.ru, 🔵 Habr Career и 🟣 Telegram (каналы внутри Telegram идут отдельно). Перед вакансией стоит 🔥 HIGH (скор выше 100), ⚡ MEDIUM (50–100) или 📄 LOW (ниже 50), затем строка совпадений whitelist. В файле у карточки остаётся 500 символов описания, в Telegram карточка короткая. В конце обоих каналов — сводка прогона и топ-3 по скору.
7. `hunter.py` — CLI для ручного прогона и отладки. Без флагов вызывает `run_search` и отправляет текст в Telegram. `--no-notify` пишет только файл и базу. `--stats` печатает таблицу `rich` и ничего не сохраняет. `--all` берёт все новые вакансии, а не десять лучших.
8. `bot.py` — долгоживущий aiogram-бот. Команды висят в меню слева от поля ввода. `/start` только здоровается и отсылает к `/help`. `/search` ищет в отдельном потоке и под ответом оставляет кнопку «📥 Скачать этот дайджест».

## Настройка .env

Единственный источник истины для конфигурации — файл `.env.example`. Файл `.env` владелец копирует и заполняет секретами самостоятельно.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Файл `.env`, каталоги `data/` и `digests/` перечислены в `.gitignore`.

## Команды бота

| Команда | Что делает |
| --- | --- |
| `/search` | Запускает поиск, присылает текстовый дайджест и кнопку скачивания этого файла |
| `/download` | Присылает самый новый файл из `digests/` |
| `/help` | Список команд |
| `/health` | Самодиагностика источников, базы, записи на диск и uptime |
| `/stats` | Число сохранённых вакансий и время последней записи по каждому источнику |
| `/add_channel` | Добавляет один или несколько каналов через пробел. Доступно только владельцу |
| `/remove_channel` | Отключает канал, строку в базе не удаляет |
| `/list_channels` | Активные каналы и сколько вакансий из них сохранено за 7 дней |

## Самодиагностика

`/health` ходит в сеть с таймаутом 5 секунд. Для `api.hh.ru` ответы `200` и `403` считаются успехом: с этого хоста API часто отвечает `403`. Пример отчёта:

```
Самодиагностика
✅ HH API — HTTP 403, для этого хоста это норма
✅ Habr RSS — HTTP 200, XML
✅ Telegram t.me/s/job_python — HTTP 200
✅ SQLite — 42 записей
✅ Запись в digests/ — файл создан и удалён
✅ Uptime — 3ч 12м 8с
```

Сбой сети или код ответа вне нормы помечается ❌.

## Запуск

```bash
.venv/bin/python hunter.py --stats
.venv/bin/python hunter.py
.venv/bin/python hunter.py --all
.venv/bin/pytest
```

`--stats` удобен для проверки с этой машины: `api.hh.ru` может ответить `403` (ddos-guard). Парсер логирует предупреждение и не роняет проход. Telegram и Хабр при этом обрабатываются как обычно.

Карточка в Telegram содержит название, оценку, зарплату и ссылку. Если токен или chat id пустые, отправка пропускается, таблица в консоли всё равно печатается.

## systemd

В корне репозитория три юнита. `vacancy-hunter.service` — долгоживущий бот (`Type=simple`, `bot.py`). `vacancy-hunter-run.service` — разовый прогон `hunter.py --no-notify`: только файл и база, без сообщения в чат. `vacancy-hunter.timer` запускает этот oneshot каждые 4 часа (`OnCalendar=*-*-* 00/4:00:00`). `Persistent=true` догоняет пропущенный запуск после простоя. Оба процесса читают `.env` из корня репозитория.

Установка:

```bash
sudo cp vacancy-hunter.service vacancy-hunter-run.service vacancy-hunter.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vacancy-hunter.service
sudo systemctl enable --now vacancy-hunter.timer
```

Управление:

```bash
systemctl status vacancy-hunter.service --no-pager
systemctl status vacancy-hunter.timer --no-pager
systemctl start vacancy-hunter.service
systemctl stop vacancy-hunter.service
systemctl start vacancy-hunter.timer
systemctl stop vacancy-hunter.timer
journalctl -u vacancy-hunter.service -n 15 --no-pager
journalctl -u vacancy-hunter-run.service -n 15 --no-pager
```

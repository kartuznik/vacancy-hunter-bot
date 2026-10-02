# vacancy-hunter

Локальный сбор удалённых вакансий с HeadHunter, публичных Telegram-каналов и RSS Хабр Карьеры. Поиск идёт обычными HTTP-запросами. На этапе отбора LLM не вызывается: решение принимает детерминированный фильтр по спискам слов и баллам.

## Архитектура

Корень репозитория — каталог с `hunter.py`. Вся предметная логика лежит в пакете `vacancy_hunter/`.

1. `hh_parser.py` делает `GET https://api.hh.ru/vacancies` по каждой фразе из `HH_QUERIES`. Параметры: `text`, `schedule=remote`, `order_by=publication_time`, `per_page=50`, `period=3`. Таймаут 10 секунд. Ответы `403` и `429` пишутся в лог, по этому запросу возвращается пустой список, остальные источники продолжают работу.
2. `tg_scraper.py` забирает HTML `https://t.me/s/<channel>` и через BeautifulSoup достаёт текст поста, дату и ссылку на сообщение. Короткие служебные блоки отбрасываются.
3. `habr_parser.py` скачивает RSS и разбирает его библиотекой `feedparser`.
4. `filter_and_score.py` смотрит название, описание и зарплату. Любое слово из `blacklist_hard` (границы слов, без учёта регистра) сразу отсекает вакансию: туда входят город, офис, гибрид и уровни senior / сеньор / lead / team lead / head / руководитель / главный / principal. Дальше нужны все три условия сразу: маркер вакансии, ядро `python` / `питон` / `пайтон` и хотя бы одна технология из whitelist. Иначе начисляются баллы: `+30` за каждое уникальное слово whitelist, `+20` за junior / intern / «без опыта» / стажёра, `+15` за явную удалёнку, `+10` если публикация моложе 24 часов, `−15` за каждое уникальное совпадение `blacklist_soft` (`middle+`, стаж 3+ года или 5 лет, английский B2, English fluent). Ядро Python в эти `+30` не входит.
5. `database.py` хранит уже показанные вакансии в SQLite. Ключ — SHA-256 от строки `источник + url`. Файл базы считается как `Path(__file__).resolve().parent.parent / "data" / "seen_vacancies.db"`. Каталог `data/` создаётся при первом запуске.
6. `hunter.py` собирает три источника, фильтрует и записывает только новые строки. Дайджест `digests/digest_YYYY-MM-DD.md` и сообщение в Telegram строит один форматтер: разделы 🟢 HH.ru, 🔵 Habr Career и 🟣 Telegram (каналы внутри Telegram идут отдельно). Перед вакансией стоит 🔥 HIGH (скор выше 100), ⚡ MEDIUM (50–100) или 📄 LOW (ниже 50), затем строка совпадений whitelist. В файле у карточки остаётся 500 символов описания, в Telegram карточка короткая. В конце обоих каналов — сводка прогона и топ-3 по скору. Локальная таблица `rich` показывает уровень, источник, канал и совпадения.

С `--all` в дайджест и Telegram попадают все новые вакансии, а не десять лучших. С `--stats` скрипт только печатает сводку и таблицу: база, файл дайджеста и Telegram не трогаются.

## Настройка .env

Единственный источник истины для конфигурации — файл `.env.example`. Файл `.env` владелец копирует и заполняет секретами самостоятельно.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Файл `.env`, каталоги `data/` и `digests/` перечислены в `.gitignore`.

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

Юниты лежат в корне репозитория: `vacancy-hunter.service` и `vacancy-hunter.timer`. Таймер запускает одноразовый сервис каждые 4 часа (`OnCalendar=*-*-* 00/4:00:00`). `Persistent=true` догоняет пропущенный запуск после простоя. Сервис стартует интерпретатором из `.venv` и сам читает `.env` из корня репозитория.

Установка:

```bash
sudo cp vacancy-hunter.service vacancy-hunter.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable vacancy-hunter.timer
sudo systemctl start vacancy-hunter.timer
```

Управление:

```bash
systemctl status vacancy-hunter.timer --no-pager
systemctl start vacancy-hunter.timer
systemctl stop vacancy-hunter.timer
journalctl -u vacancy-hunter.service -n 15 --no-pager
```

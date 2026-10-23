# AdReel — памятка для работы с кодом

Telegram-бот: из материалов о товаре собирает рекламный AI-ролик до 30 с
(стартовый кадр → image-to-video на fal.ai → озвучка ElevenLabs → монтаж FFmpeg).
План по этапам — `docs/PLAN.md`, принятые решения — `docs/DECISIONS.md`.
Прототип, откуда перенесены модули: `C:\Users\timur\VSCodeProjects\AI_Host` (videonews/).

## Команды
- `uv sync` — зависимости (Python 3.12)
- `uv run pytest` — тесты; `-m live` + `RUN_LIVE_TESTS=1` — с платными API
- `uv run ruff check .` и `uv run mypy adreel` — должны быть чистыми
- `uv run python -m adreel.bot` — бот локально; `docker compose up -d --build` — бот + локальный Bot API
- `run_bot.bat` — то же двойным кликом: проверит uv/ffmpeg/.env, перезапустит после падения;
  `run_bot.bat docker` — через compose

## Структура
- `adreel/config.py` — настройки из `.env`, YAML из `config/`
- `adreel/net.py` — HTTP с повторами (не для Claude и fal: у них свои SDK с ретраями)
- `adreel/llm/` — `LLMProvider`: Claude (`messages.parse`), DeepSeek (JSON + переспрос), Fake
- `adreel/tts/` — `TTSProvider`: ElevenLabs с таймингами и защитой от обрыва, Fake
- `adreel/gen/` — fal.ai: `FalGateway` (очередь + защита от двойной оплаты), каталог моделей, Fake
- `adreel/render/` — ffmpeg (probe, concat), ASS-субтитры, `montage.py` — сборка
  готового ролика: сегменты, голос, музыка с ducking, субтитры
- `adreel/db/` — модели SQLAlchemy, сессии, запуск миграций (`migrations/`)
- `adreel/ingest/` — страницы товара, картинки, видео (yt-dlp), голосовые (Scribe), хранилище файлов
- `adreel/product/` — карточка (`schema.py`), вопросы, сборка карточки LLM (`extract.py`),
  `ProductService` — вся логика товара; бот только вызывает его
- `adreel/music/` — разбор трека (`analyze.py`, librosa), кусок словами (`segment.py`,
  в т.ч. несколько кусков подряд — `parse_parts`), `MusicService` — библиотека, выбор
  куска, превью
- `adreel/script/` — креатив: схемы (`schema.py`), агенты с переделкой по ошибкам
  (`creative.py`), правила (`validate.py`), оператор — промпты под модель и непрерывность
  сцен (`shots.py`), `ScriptService` — направления, сценарий, озвучка, план оператора.
  Ролик — история: у направления сюжет по шагам (`beats`), у сцены событие (`event`),
  крупность, место и герои (`cast`); `story_scene` — сцена без товара (кадр по тексту).
  Разнообразие держит `validate.py`, а не надежда на промпт
- `ShotPlans.needs_tail` — монтаж берёт конец клипа, если там переход или продолжение;
  `pacing_line` говорит модели, когда случается действие (клип длиннее сцены)
- `adreel/script/playbook.py` + `config/playbook.yaml` — проверенный опыт по AI-роликам
  (что модель тянет, приёмы склейки, ритм, уроки). Уходит в промпты креативщика и
  режиссёра: правим знание в одном месте, видят все агенты
- `adreel/research/` — поиск (Tavily) и рыночная выжимка для креативщика; без ключа шаг
  пропускается. Интернет даёт идеи, факты о товаре — только из карточки
- `prompts/*.md` — промпты, первая строка `<!-- version: имя_vN -->`
- `adreel/gen/pipeline.py` — `GenerationService`: оценка, генерация сцен по очереди,
  `assemble()` — монтаж. Владельцу уходит один готовый ролик, куски не шлём
- Порядок в боте: товар → креатив (`creative_flow.py`) → музыка (`music_flow.py`)
  → итоговый экран → генерация и монтаж (`creative_flow.run_generation`)
- `adreel/bot/` — `app.py` (сборка), `product_flow.py` (товар), `music_flow.py` (музыка),
  `view.py` (тексты и кнопки), `storage.py`. Роутер товара подключается последним:
  в нём обработчики «всё остальное»
- `adreel/costs.py`, `adreel/pace.py`, `adreel/health.py`
- Новая таблица или поле: правка `db/models.py` +
  `uv run alembic revision --autogenerate -m "..."` (проверь сгенерированное глазами)

## Правила
- Внешние сервисы — только через Protocol-интерфейсы; у каждого есть Fake для тестов.
- В тестах НЕ вызывать платные API. Живые тесты помечать `@pytest.mark.live`.
- Ключи — только `.env`. Цены — только `config/pricing.yaml` и `config/video_models.yaml`.
  Имена моделей — `.env` и `config/`, не в коде.
- Каждый платный вызов пишет событие через `costs.record(...)`.
- Любой запуск генерации запоминается (`gen/pending.py`) до ожидания результата —
  повторная попытка должна подбирать оплаченное, а не платить заново.
- Звук главный: клипы генерируются не короче нужного и режутся под озвучку.
- Качество важнее цены: по умолчанию генерим в 1080P. Дёшево — только живые проверки
  схемы (`VIDEO_MODEL=h3-max-turbo-480`).
- Формат промпта к видеомодели собирает КОД (`script/shots.build_prompt`), агент даёт
  только смысловые куски. Правила формата держатся проверками, а не надеждой на промпт.
- Комментарии объясняют «зачем» и уроки из прототипа, а не пересказывают код.
- Решения и допущения — в `docs/DECISIONS.md`.

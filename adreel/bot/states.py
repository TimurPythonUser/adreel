from aiogram.fsm.state import State, StatesGroup


class Flow(StatesGroup):
    choosing = State()     # продолжить со старым товаром или новый
    collecting = State()   # принимаем материалы
    busy = State()         # идёт разбор или правка — ждём
    asking = State()       # вопрос анкеты
    reviewing = State()    # карточка на подтверждение
    editing = State()      # ждём правку словами
    updating = State()     # старый товар: что изменилось
    ready = State()        # карточка подтверждена

    creative_busy = State()  # креативщик / режиссёр / копирайтер думают
    directions = State()     # выбор креативного направления
    direction_own = State()  # ждём свой вариант направления словами
    direction_check = State()  # своё направление оформлено — берём или иначе
    duration = State()       # выбор длины ролика
    script_review = State()  # сценарий видео на утверждение
    script_edit = State()    # ждём правку сценария словами
    voice_review = State()   # озвучка на утверждение
    voice_edit = State()     # ждём правку озвучки словами
    final_review = State()   # итог: сценарий + озвучка + трек, утвердить или поменять
    generating = State()     # идёт платная генерация кадров
    done = State()           # готовый ролик отправлен

    music = State()          # ждём трек: файл, ссылка YouTube или выбор из библиотеки
    music_busy = State()     # качаем и разбираем трек
    music_preview = State()  # послушать кусок: беру / другой кусок / свой кусок
    music_segment = State()  # ждём, какой кусок взять словами
    music_rename = State()   # ждём новое название трека
    music_done = State()     # музыка выбрана

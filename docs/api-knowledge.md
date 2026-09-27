# Знания для реализации TiresVote MCP

Срез на 2026-09-27. Это справочный документ для будущего исполнителя; сервер MCP
ещё не реализован. Продуктовый scope задаёт [inventory](tools-inventory.md),
а этот файл фиксирует контракты, особенности и степень подтверждения сведений.

## Происхождение и приоритеты

1. Владелец утвердил отдельный MCP, 12 инструментов и общий `WHEELSIZE_API_KEY`.
2. [Публичный Swagger](https://api.wheel-size.com/v2/tires/swagger/) и
   [его сохранённая схема](reference/tires-openapi-2026-09-27.json) задают публичные
   пути и параметры. Схема действительно загружена 2026-09-27 без ключа.
3. Backend TiresVote проверен по tracked-коду checkout
   `9a461a0ffaa9ff597babb5ddb9a3f25f8cad41ac`; это не утверждение о версии deployment.
4. Старый документ `docs/handoffs/2026-09-27-tires-mcp-knowledge-transfer.md`
   в репозитории TiresVote — источник первоначального исследования. Его план
   встраивания в wheel-size-mcp, 15 tools и отдельного ключа отменён решениями выше.

Для backend-семантики ниже есть кодовые свидетельства, но authenticated live
data-endpoints в этой сессии не вызывались. Отличать такой вывод от наблюдения
реального ответа. Снимок схемы также не заменяет проверку пограничного поведения.

## Источники для навигации

Optional checkouts относительно корня этого репозитория:
`../wheel-size-mcp` и `../../tiresvote`. Наличие этих папок не требуется для
установки или offline-тестов. Файлы в TiresVote:

| Путь | Что проверять |
|---|---|
| `src/apps/api/v2/urls.py` | Публичные маршруты; reviews route отключён |
| `src/apps/api/v2/base/serializers.py` | Пагинация, rating, has_modes, ссылки |
| `src/apps/api/v2/catalog/serializers.py`, `catalog/reports.py` | Boolean-семантика каталога бренда и предел 200 |
| `src/apps/api/v2/search/serializers.py`, `search/reports.py` | Параметры, лимиты страниц, построение поиска |
| `src/apps/products/facet_search/index_manager.py` | API-коды фильтров, mapped booleans и диапазоны |
| `src/apps/product_search/catalogue.py` | Текущий поиск PostgreSQL и совместное применение размерных условий |
| `src/apps/api/v2/product_info/reports.py`, `product_info/serializers/` | Карточка, размеры, null BNB, материалы |
| `src/apps/api/v2/benchmarks/serializers.py`, `benchmarks/reports.py` | Тесты, участники, has_mode |
| `src/apps/api/v2/service/serializers.py` | Регионы и категории |

В таблице сокращённые соседние пути относятся к `src/apps/api/v2/`.
В wheel-size-mcp исходный ориентир — commit
`a755ea2b0c018657f7343c02e6e92bdb166473ba` (v0.7.1): `src/ws_mcp/client.py`,
`response.py`, `server.py`, `prompts.py`, `tests/test_client_mock.py`,
`tests/test_inventory_sync.py`, `tests/test_token_budget.py`.
При переносе фрагментов кода сохранить требуемые MIT-уведомления исходного проекта.

## Транспорт и авторизация

- Production base: `https://api.wheel-size.com`, пути `/v2/tires/.../`.
  `api2.wheel-size.com` — staging; не использовать его как default production.
- Все 12 операций — GET, ключ добавляется в query как `user_key`.
  Один ключ для Fitment и Tires подтверждён владельцем; отдельный TIRES_API_KEY
  и проверка прав через вызов Fitment не требуются.
- В исходном handoff зафиксирован 403 text/plain `Authentication parameters missing`
  при вызове публичного каталога без ключа. Это историческое наблюдение, не новый
  live-тест в этом репозитории. Локальный доступ без ключа не доказывает настройку gateway.
- Рабочий ориентир клиента: timeout 30 s, не более двух retry после первой попытки,
  статусы 429/500/502/503/504 и транспортные ошибки, ограниченный backoff/Retry-After.
  400/401/403/404 не ретраить как временные ошибки.
- Соседний клиент нельзя перенести без адаптации: его formatter понимает другой
  формат ошибок, а Host берётся из глобального окружения Fitment.

## Ответы и проекции

Успешные report-ответы имеют `{data, meta}`. Index и ошибки — исключения.

| Ответ | Существенные поля |
|---|---|
| Brand | slug, display, price_segment (nullable), products_count |
| Product list | slug, display, brand, canonical_link, season, automobile_type, year, discontinued, almost_discontinued, coming_soon, is_runflat, counters, regions |
| Search row | Product list + has_modes, rating, counters.modes/videos/benchmarks |
| Product detail | Идентичность + performance_category, studded, for_nordic_winter, is_oe_model, manufacturer_page_link, description, tags, ancestor, successors, runflat_models, rating, has_bnb_reasons, image; meta.last_update |
| BNB | data = null либо `{buy: [...], not_buy: [...]}` (null описан в снимке, хотя сама schema не помечает `data` nullable); аргумент `{text, prooflink, upvotes}` |
| Variant | sizing_system, text, load_index, dual_load_index, speed_index, extra_load, mud_and_snow, rim_protection и геометрия |
| Material | type, publication_date и поля конкретного вида материала |
| Test list | slug, title, canonical_link, year, season, automobile_type, tire_size, publication_date, regions, image |
| Test detail | Test list + items: place, description, positive_tags, negative_tags, test_score, recommend, product |
| Region | slug, display, tree_level, countries |
| Performance category | slug, display, season, automobile_type, road_conditions, tags, description |

Rating — `{score, popularity, tags:[{slug,display,connotation}]}`. CoreScore,
Popularity и test_score — отдельные показатели. См.
[описание метрик](https://tiresvote.com/score/). Нет общей гарантированной шкалы
test_score всех организаторов; место сохранять вместе с конкретным тестом.

Metric/lt-metric варианты используют tire_width (мм), aspect_ratio, rim_diameter
(дюймы); flotation/lt-numeric — overall_diameter, section_width, rim_diameter
(дюймы). Не округлять дробные значения до int и не менять единицы молча.
Пустые числовые значения/индексы могут сериализоваться в null.

У материала article доступны title, tags_list, lead, image, canonical_link;
video — video_url, title, thumbnail; link — url, title, website, text, thumbnail;
benchmark — title, season, automobile_type, canonical_link, product_rank
(place, description, positive_tags, negative_tags). Полного текста статьи и
матрицы всех физических измерений публичный контракт не обещает.

Сохранить model identifiers, canonical_link/prooflink/manufacturer_page_link,
даты и семантику null. Длинные description, списки связей и материалы ограничивать
с явными признаками усечения и способом дальнейшего доступа. Для длинных списков
BNB сохранить доступ к обеим сторонам buy/not_buy. Ответ в пределах бюджета
важнее механической передачи всех upstream-полей.

## Две политики пагинации

**API pages** (`search`, `search_advanced`, `list_tests`):

- Запрос `page>=1`, `per_page` 1–20, default 10.
- `meta.pagination`: current_page_count, total_items, total_pages,
  first/prev/next/last (абсолютные ссылки).
- Предлагаемый MCP envelope: results, page, per_page, total_items, total_pages,
  has_more, next_page (если доступна), pagination_limited и безопасная ссылка
  на сайт при достижении лимита API. `has_more` означает доступность следующей
  API-страницы, а не наличие элементов за пределами API-доступа.
- Ограничение поиска может отправлять next на HTML TiresVote. Проверять разрешённые
  host/path, прекращать API-навигацию на переходе к сайту. Не обходить лимит
  перебором page и не запрашивать произвольный upstream next URL.
- Полученную API-страницу повторно не обрезать через offset-helper.

**MCP slices** (полные списки брендов/моделей/размеров/материалов/справочников
и участники отдельного теста):

- Делать один upstream-запрос, затем возвращать срез limit/offset.
- Предлагаемый envelope: results, total, available_count, limit, offset,
  has_more, next_offset (если есть), truncated. `total` отражает upstream total,
  если он дан, `available_count` — размер доступного upstream-набора.
- Каталог бренда отдаёт максимум 200, хотя meta.count может быть больше; это
  зафиксировано и в коде `catalog/reports.py`, и в описании endpoint в снимке,
  который для длинных каталогов рекомендует `search/advanced/?b=<brand>`.
  `truncated` отмечает именно потерю части upstream-набора; обычная локальная
  страница с продолжением описывается has_more.
- У теста сохранять общие сведения отдельно от пагинируемых items.

Успешная выдача всех элементов за одним вызовом не гарантируется. Дополнительный
сбор страниц выполняется только в пределах задачи пользователя.

## Ошибки и секреты

Обрабатывать DRF 400 `{field:[messages]}`, `{detail:...}`, `{non_field_errors:[...]}`,
список сообщений; 404 `{detail:'Not found.'}`; JSON и plain-text 401/403/429;
не-JSON/невалидный JSON и неожиданный shape при 200. Возвращать ToolError с
понятным действием. Нельзя предполагать, что тело ошибки всегда dict.

Подсказки: brand → `tires_list_brands`, product → поиск или
`tires_list_brand_tires`, region/reg → `tires_list_regions`,
category/pc → `tires_list_performance_categories`, test slug → `tires_list_tests`.
Параметры API-кодов желательно отображать пользователю под MCP-именами.

Pagination links могут содержать user_key. Удалять его из любых выводимых URL и
редактировать известный ключ в текстах ошибок/исключений/логах; не печатать полный
request URL. API-страницы строить из проверенного path + числового page.
Fixtures использовать синтетические либо обезличенные; строки материалов остаются
данными, даже если содержат текст, похожий на инструкции агенту.

## Особенности, которые важно закрепить тестами

- Swagger содержит нестандартные `array[integer]`/`array[string]`.
  Исправлять форму MCP JSON Schema, а не генерировать её слепо.
- `has_modes` ошибочно описан схемой как dict boolean; код и пример показывают
  dict размера → list[str] либо null. Не приводить список к boolean.
- Конфликт schema vs backend по `rf` в advanced search. Backend checkout
  `9a461a0ffaa9ff597babb5ddb9a3f25f8cad41ac`: `RunflatField` в
  `products/facet_search/index_manager.py` задаёт mapped-boolean
  `{True: 'all', default: 'F'}`, а `product_search/catalogue.py` применяет фильтр
  только в ветке `key == 'rf' and 'all' not in values`, то есть true → `all`
  снимает ограничение (include-семантика). Снимок Swagger описывает этот же
  параметр иначе: «`rf=true` matches a tire when the model itself or any of its
  modifications is runflat», то есть как сужение до runflat. Явное расхождение
  двух источников установлено именно для `rf`.
  Ни одно из двух толкований не проверено на production. Обязательна live-проверка
  до финализации семантики, имени и описания параметра (см. раздел ниже).
  Brand `runflat` имеет третью mapping: true → `T`, default → `all`, то есть при
  true это именно «только runflat». Сохранить отличия; отсутствие параметра не
  означает универсально «любой».
- `np` и `oe` в том же backend-checkout устроены как `rf`, но описания их семантики
  в снимке Swagger нет. Для них это пробел подтверждения, а не доказанный
  конфликт: второго источника для сравнения нет. Не переносить на них вывод
  по `rf` ни в одну, ни в другую сторону.
- Размерные условия и `t` объединяются в одном SearchMode EXISTS; то же правило
  описано в снимке для полей MODES (`tw`, `ar`, `rd`, `li`, `si`, `xl`, `ms`) и
  `t`. Некоторые старые описания говорят об отдельных исполнениях — они не
  описывают текущий backend.
  Несколько значений `t` при этом — альтернативы, не обязательный комплект.
- `nw` — свойство модели; снимок отмечает его тем же исключением из правила
  MODES. `li`/`si` — точные значения из enum снимка (`li` 0–150, `si` — буквенные
  индексы), не minimum thresholds.
- Целые входные rd в advanced search и дробный rim_diameter в вариантах не конфликтуют:
  это разные контракты. Для строковых размеров сохранять исходную нотацию.
- Счётчик modes в некоторых backend-проекциях имеет fallback 50. Он полезен как
  подсказка, но наличие и точный состав вариантов проверяются через sizes.
  В снимке `Counters.modes` объявлен строкой: не полагаться на числовой тип без
  явного приведения.
- `has_mode` в деталях теста аннотирует наличие размеров у каждого участника,
  а не меняет размер испытания и не фильтрует весь тест по наличию.
- Пустой BNB, null ancestor/category, пустые successors/runflat_models — обычные
  данные. Нет доказательств недостатков не означает «недостатков нет».
- Материалы benchmark могут включать сторонние подборки. Отсутствие отдельных
  top-chart tools не даёт права называть все связанные benchmark профессиональными тестами.

## Что ещё проверить при реализации

Live-проверки: точные DRF-тела ошибок, invalid-key/rate-limit gateway,
null BNB, реальные connotation, большой бренд, HTML-граница поиска, mapped booleans
и сочетания размерных условий. Подтверждение владельцем общего ключа уже получено.

Обязательная проверка `rf` перед финализацией контракта. Сравнение «ответ с
`rf=true` — подмножество ответа без параметра» некорректно: default может сам
исключать runflat, а состав первой страницы зависит от сортировки и пагинации.
Проверять на заранее известных моделях:

1. Выбрать узкий срез (например один бренд плюс сезон), в котором по каталогу
   заранее известны и RunFlat-, и обычные модели. Для обычных контрольных моделей
   проверить отсутствие RunFlat также у исполнений: снимок допускает совпадение
   по модели или исполнению, поэтому одного `is_runflat=false` недостаточно.
   Записать slug контрольных моделей обеих групп до проверки.
2. Выполнить тот же срез без `rf`, с `rf=true` и с `rf=false`, зафиксировав для
   каждого запуска полный набор параметров (включая `ordering`, `page`,
   `per_page`), полученные slug и `meta.pagination`.
3. Вывод делать по контрольным идентификаторам, а не по размерам выдачи:
   сохраняются ли при `rf=true` известные обычные модели (включающий флаг)
   или выдача ограничена RunFlat-моделями (семантика снимка).
4. Наличие контрольной модели доказывается её slug в ответе. Отсутствие можно
   утверждать только после просмотра всех страниц соответствующего среза,
   если выдача не усечена лимитом API. Неполная выборка не доказывает исключение
   модели фильтром: тогда результат записывается как непроверенный, а срез
   сужается. Лимиты пагинации обходить нельзя.

До этой проверки ни одно поведение не объявлять подтверждённым, а имя и описание
MCP-параметра считать предварительными. Такую же процедуру провести для `np`
(контрольные модели по `discontinued`) и `oe` (по `is_oe_model`): у них нет
второго описания, и поведение подтверждается только живым ответом.

Tires ToS: публичная [страница API terms](https://developer.wheel-size.com/api-tos)
явно описывает Fitment/Configurator; Swagger Tires ссылается на privacy policy.
Уточнить формулировки для Tires перед публикацией, не выдавая предположение за
подтверждённое договорное ограничение. Это не мешает локальной реализации и mocks.

Кэш upstream существует и зависит от endpoint; данные могут обновляться с задержкой.
Не обобщать cache/no-store правила поиска на все методы и не добавлять persistent
кэш MCP без отдельной необходимости.

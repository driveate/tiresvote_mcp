# Tools inventory — первая версия

Статус: утверждённый список инструментов и проект их параметров; регистрации MCP
ещё нет. При реализации дополнить каждую секцию точным английским docstring и
описаниями `Field`, затем проверять их синхронность тестом. Названия инструментов
и исключения ниже — согласованный объём продукта.

Все пути ниже начинаются с `/v2/tires/` и вызываются методом GET.
`brand`, `product`, `slug` получают из ответов каталога/поиска; имя модели не
гарантирует совпадение с её slug. Нотацию размера передают без slug-нормализации.

| Инструмент | Относительный API path | Модуль |
|---|---|---|
| `tires_list_brands` | `/catalog/` | catalog |
| `tires_list_brand_tires` | `/catalog/{brand}/` | catalog |
| `tires_get_tire` | `/catalog/{brand}/{product}/` | catalog |
| `tires_list_sizes` | `/catalog/{brand}/{product}/modes/` | catalog |
| `tires_search` | `/search/` | search |
| `tires_search_advanced` | `/search/advanced/` | search |
| `tires_get_pros_cons` | `/catalog/{brand}/{product}/bnb/` | evidence |
| `tires_list_materials` | `/catalog/{brand}/{product}/materials/` | evidence |
| `tires_list_tests` | `/tests/` | evidence |
| `tires_get_test` | `/tests/{slug}/` | evidence |
| `tires_list_regions` | `/regions/` | catalog |
| `tires_list_performance_categories` | `/performance-categories/` | catalog |

Исключённые upstream paths: `/articles/`, `/top-charts/`, `/top-charts/{slug}/`.
Общий read-any-path инструмент также не входит в интерфейс.

## Общие параметры

- API-пагинация: `page: int = 1` (>=1), `per_page: int = 10` (1–20).
- MCP-пагинация полных upstream-списков: `limit: int = 20` (1–50),
  `offset: int = 0` (>=0). Эти параметры в upstream не отправляются.
- Массивы: нормальная MCP JSON Schema `array` + `items`; HTTP — повторяющиеся
  query keys. Пустой массив лучше отклонить с подсказкой убрать параметр.
- Необязательные boolean передаются только если заданы; `False` сохраняется.
- Сортировка `ordering`: поля `popularity`, `score`, `slug`, необязательный `-`,
  запятая как разделитель; default upstream `-popularity,-score,slug`.

## `tires_list_brands`

Фильтр `price_segments: list[str] | None` → `price_segment`.
MCP `limit/offset`. Ответ: slug, display, price_segment, products_count.
Сегменты `premium`, `mid-range`, `economy` присутствуют в снимке схемы;
значения справочника могут меняться, не превращать снимок в вечный enum.

## `tires_list_brand_tires`

Обязателен `brand: str`. Фильтры:

| MCP | API | Тип / смысл |
|---|---|---|
| `regions` | `region` | `list[str]`, рынки TiresVote |
| `seasons` | `season` | `list[str]`: summer, all, winter |
| `automobile_type` | `automobile_type` | car или suv |
| `runflat` | `runflat` | bool; true = только RunFlat по сериализатору каталога бренда и описанию снимка |
| `include_discontinued` | `show_discontinued` | bool; true включает снятые модели |
| `include_oe` | `show_oe` | bool; true включает OE-модели |
| `ordering` | `ordering` | Общая сортировка |

MCP `limit/offset`. Upstream возвращает максимум 200 моделей. Ответ отдельно
сообщает `total`, `available_count`, `truncated`; следующая MCP-страница возможна
только внутри реально полученного набора. Для дальнейшего поиска предложить
уточнить фильтры или использовать `tires_search_advanced(brands=[...])`.

## `tires_get_tire`

Обязательны `brand`, `product`. Предлагаемый локальный параметр
`detail: Literal['concise', 'full'] = 'concise'` управляет проекцией.
Оба режима сохраняют идентичность, каноническую ссылку, статусы, категорию,
CoreScore/Popularity и `last_update`. Полный режим добавляет доступное описание,
заявленные признаки и изображения в пределах бюджета ответа.
Связи ancestor/successors/runflat_models позволяют перейти к другой модели.
`full` означает полноту выбранных полей, а не снятие лимита длины.

## `tires_list_sizes`

Обязательны `brand`, `product`; MCP `limit/offset`.
Ответ сохраняет `sizing_system`, `text`, геометрию, индексы нагрузки/скорости,
XL, M+S и защиту обода. Дробные диаметры допустимы в ответе.
Это известные актуальные исполнения из каталога; снятые исполнения upstream
исключает. Проверять складское наличие этот инструмент не может.

## `tires_search`

`query: str` обязателен, максимум 100 символов (`QuerySearchFilterSerializer` в
`src/apps/api/v2/search/serializers.py` репозитория TiresVote; в снимке Swagger
`maxLength` потерян); API `page/per_page`.
Поиск используется для разрешения названия в `brand` + `product`, включая случаи
нескольких похожих моделей. Ответ содержит компактные карточки, рейтинг и ссылки.

## `tires_search_advanced`

Все фильтры необязательны; API `page/per_page` и `ordering`.

| MCP | API | Тип / смысл |
|---|---|---|
| `brands` | `b` | `list[str]` |
| `regions` | `reg` | `list[str]`, рынки TiresVote |
| `seasons` | `s` | `list[str]`: summer, all, winter |
| `automobile_types` | `at` | `list[str]`: car, suv |
| `performance_categories` | `pc` | `list[str]` из справочника |
| `price_segments` | `ps` | `list[str]`, сегмент бренда |
| `production_years` | `y` | `list[int]`, 1900…текущий год+2 (в снимке 2028) |
| `tire_widths` | `tw` | `list[int]`, 95–525 мм |
| `aspect_ratios` | `ar` | `list[int]`, 20–95 |
| `rim_diameters` | `rd` | `list[int]`, 10–32 дюйма |
| `speed_indices` | `si` | `list[str]`, точные значения |
| `load_indices` | `li` | `list[int]`, 0–150, точные значения, не нижняя граница |
| `sizes` | `t` | `list[str]`, например `225/45R17`; исходная нотация |
| `include_discontinued` | `np` | bool; по коду true включает снятые модели; в снимке не описано |
| `include_runflat` | `rf` | bool; имя предварительное — источники расходятся, см. ниже |
| `include_oe` | `oe` | bool; по коду true включает OE-модели; в снимке не описано |
| `extra_load` | `xl` | bool, свойство исполнения |
| `mud_and_snow` | `ms` | bool, свойство исполнения |
| `nordic_winter` | `nw` | bool, свойство модели |

По `rf` источники явно расходятся: backend даёт включающий флаг (true → `all`),
а снимок Swagger описывает `rf=true` как отбор только runflat-моделей. У `np` и
`oe` описания в снимке нет вообще: их include-семантика опирается только на код
и остаётся неподтверждённой вторым источником, а не опровергнутой.
Конфликт, пробел подтверждения и методика обязательной live-проверки описаны в
[API knowledge](api-knowledge.md). До проверки имена `include_*`, их описания и
соответствующие строки этой таблицы считать предварительными и не объявлять ни
одно поведение подтверждённым. В любом случае это не тот же параметр, что `runflat`
каталога бренда (true → `T`, только runflat).

Список `sizes` ищет альтернативы (OR), а не обязательное наличие всех размеров.
Для комплекта разных размеров подтвердить каждый размер у выбранной модели.
Размерные поля объединяются на одном исполнении; не выдавать совпадения разных
исполнений за один подходящий вариант. Параметры с индексами задают точные
значения; требования «не ниже» нельзя интерпретировать как равенство.

Сохранять `has_modes`: запрошенный размер → список совпавших обозначений или null.
Он помогает проверить размер, но не заменяет все проверки исполнения.

## `tires_get_pros_cons`

Обязательны `brand`, `product`. Ответ сохраняет `buy`, `not_buy`, а у аргументов —
`text`, `prooflink`, `upvotes`. `data: null` означает отсутствие одобренных
аргументов. `has_bnb_reasons` из карточки позволяет избежать лишнего вызова.
При необходимости ограничения длинных списков добавить явную навигацию по
сторонам buy/not_buy; финальную сигнатуру закрепить в inventory и тестах.

## `tires_list_materials`

Обязательны `brand`, `product`; `material_type` → `type`:
article, video, benchmark, link; MCP `limit/offset`.
Возвращать тип, название, дату, источник и доступное краткое содержание.
Материал benchmark не обязательно профессиональный тест: тип upstream объединяет
несколько видов сравнений. Не переименовывать каждый такой материал в pro test.
Связанные статьи разрешены; общий каталог статей и top-chart tools исключены.

## `tires_list_tests`

`years: list[int] | None` → `year` (1900…текущий год+2),
`seasons: list[str] | None` → `season`, `automobile_type` (car/suv),
API `page/per_page`. Ответ: slug, название, год, сезон, тип авто, размер испытания,
регионы, дата публикации и ссылка.
Upstream не имеет фильтра списка по размеру, бренду или издателю: не обещать
поиск полного набора тестов по этим признакам одним вызовом.

## `tires_get_test`

Обязателен `slug`; `sizes: list[str] | None` → `has_mode`, каждое значение не длиннее
30 символов (`BenchmarkDetailFilterSerializer`; то же `maxLength` есть в снимке).
MCP `limit/offset` ограничивают только участников, сохраняя общие сведения теста.
Участник: место, test_score, recommend, вывод, плюсы/минусы и идентичность модели.
`has_mode` проверяет наличие запрошенного размера у участников; это не фильтр
размера испытания и не обещание, что все возвращённые участники его имеют.

## `tires_list_regions`

MCP `limit/offset`. Сохранить slug, display, tree_level, countries.
Использовать справочник TiresVote, включая его иерархию и агрегированные рынки.

## `tires_list_performance_categories`

MCP `limit/offset`. Сохранить slug, display, season, automobile_type,
road_conditions, tags и ограниченное описание назначения.

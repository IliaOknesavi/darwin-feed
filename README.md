# darwin-feed

YML-фид каталога питомника «Сажень» (darwinshop.ru, Томск) для автообновления сайта.

## Ссылки для импортёра

| Ссылка | Что это |
|---|---|
| https://iliaoknesavi.github.io/darwin-feed/darwinshop.xml | **Рабочий фид.** Весь каталог; у части позиций — наши описания под условия Томской области |
| https://iliaoknesavi.github.io/darwin-feed/darwinshop-snapshot.xml | Снимок текущего наполнения сайта без наших переработок. Страховка на откат |

Ссылки постоянные, содержимое перезаписывается при каждой публикации.

## Формат

YML (Yandex Market Language), UTF-8, с DOCTYPE. Ключ сопоставления товара —
атрибут `id` предложения, это **Артикул**. У каждого предложения: `url`, `price`,
`currencyId`, `categoryId`, `name`, `description`, `picture` ссылками и характеристики
парами `param name="…"`.

Отдаётся через GitHub Pages, а не raw.githubusercontent: raw возвращает файл
с `Content-Type: application/octet-stream` независимо от расширения, Pages отдаёт
`.xml` как `application/xml`.

## Как обновляется

Содержимое генерируется в проекте darwin_rag (`scripts/build_yml.py`) и выкладывается
сюда командой `bash scripts/publish_feed.sh`. Репозиторий существует ради одного —
дать постоянный адрес.

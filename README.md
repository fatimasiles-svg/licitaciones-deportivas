# Panel de licitaciones deportivas (bolsas del corredor, carreras, eventos)

Cada lunes, revisa solo el feed ATOM abierto de PLACSP (Plataforma de
Contratación del Sector Público), filtra por las palabras clave de vuestro
negocio y publica el resultado en **una página web propia** que podéis
consultar desde el móvil o el ordenador, sin instalar nada ni tocar código.
Opcionalmente, también puede avisaros por email o Telegram.

**La única cosa que necesitas es una cuenta gratuita de GitHub** — el resto
(la ejecución semanal y la web) lo hace todo solo. No hace falta certificado
digital ni registrarte en PLACSP para leer el feed: es público.

Si en algún momento quieres tocar algo del código (por ejemplo para probar
cambios antes de publicarlos), sí necesitarás Python 3.9+ en tu ordenador —
ver el punto 5, opcional.

## 2. Publicar vuestro panel web (los únicos pasos necesarios)

1. **Crea una cuenta en [github.com](https://github.com)** si no tenéis una
   (gratis, un par de minutos).
2. **Crea un repositorio nuevo** desde github.com → botón verde "New" →
   ponle un nombre, por ejemplo `licitaciones-deportivas` → puede ser
   **privado** (solo lo veréis vosotros) → "Create repository".
3. **Sube esta carpeta al repositorio.** La forma más sencilla, sin usar la
   terminal, es arrastrar todos los archivos de `licitaciones-deportivas/`
   a la página del repositorio en GitHub ("uploading an existing file") y
   confirmar. Si prefieres la terminal:
   ```bash
   cd licitaciones-deportivas
   git init
   git add .
   git commit -m "Panel de licitaciones deportivas"
   git branch -M main
   git remote add origin https://github.com/TU_USUARIO/licitaciones-deportivas.git
   git push -u origin main
   ```
4. **Activa la web (GitHub Pages):** en el repositorio, ve a **Settings →
   Pages**. En "Build and deployment" → "Source" elige **"Deploy from a
   branch"**, y en "Branch" selecciona **`main`** con la carpeta **`/docs`**
   → Save. GitHub te dará la URL de vuestra web (algo como
   `https://TU_USUARIO.github.io/licitaciones-deportivas/`) en un par de
   minutos.
5. **Lánzalo una primera vez a mano** para no esperar al lunes: pestaña
   **Actions** → "Monitor licitaciones deportivas" → botón **"Run
   workflow"**. Tarda menos de un minuto. Cuando termine, refresca la URL
   del paso 4 y ya tenéis vuestro panel con los resultados.

A partir de aquí no tenéis que hacer nada más: todos los lunes se actualiza
solo, y la web siempre muestra la última comprobación y el histórico de
licitaciones encontradas.

## 3. (Opcional) Añadir avisos por email o Telegram

Si además de consultar la web queréis un aviso automático (email o Telegram)
en cuanto salga algo nuevo, en vez de tener que acordaros de mirar el panel:

### Email (Gmail)
1. Activa la verificación en dos pasos en la cuenta de Gmail que vayáis a usar.
2. Crea una "contraseña de aplicación" en https://myaccount.google.com/apppasswords
3. Usa esa contraseña de 16 caracteres, no la contraseña normal de la cuenta.

### Telegram (alertas instantáneas al móvil)
1. Habla con [@BotFather](https://t.me/BotFather) en Telegram, crea un bot con
   `/newbot` y copia el token que te da.
2. Escríbele algo a tu bot nuevo (para que sepa quién eres).
3. Visita `https://api.telegram.org/bot<TU_TOKEN>/getUpdates` y busca tu
   `chat.id` en la respuesta.

### Dónde meter esos datos
En el repositorio: **Settings → Secrets and variables → Actions → New
repository secret**, y añade solo los que vayáis a usar:
- `EMAIL_USER`, `EMAIL_PASSWORD`, `EMAIL_TO`
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`

No hace falta tocar ningún archivo ni volver a subir nada: el workflow ya
está preparado para leer estos Secrets automáticamente la próxima vez que
corra.

## 4. Cómo funciona por dentro (no necesitas leer esto para usarlo)

El workflow de `.github/workflows/licitaciones.yml` corre todos los lunes a
las 07:00 UTC (o cuando lo lances a mano): descarga el feed de PLACSP,
filtra por vuestras palabras clave, guarda qué licitaciones ya visteis en
`data/vistos.json` y el histórico completo en `data/historial.json`, y
regenera `docs/index.html` (vuestra web) con los resultados. Todo eso se
commitea solo al repositorio para que la próxima ejecución recuerde el
estado anterior.

Nota: no subáis nunca un `config.yaml` con contraseñas reales dentro a un
repositorio público; usad los Secrets de GitHub como se explica arriba. Si
el repo es privado no pasa nada por tener el resto de la configuración
(palabras clave, CPV) en claro — no es información sensible.

## 5. (Opcional, para probar cambios antes de subirlos) Correrlo en vuestro ordenador

No hace falta para el uso normal (eso ya lo hace GitHub Actions), pero es
útil si queréis probar un cambio en las palabras clave antes de subirlo.
Necesita Python 3.9+:

```bash
cd licitaciones-deportivas
python3 -m venv venv
source venv/bin/activate        # en Windows: venv\Scripts\activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
python monitor_licitaciones.py --config config.yaml --dry-run
```

`--dry-run` solo muestra por consola lo que encontraría, sin enviar nada ni
tocar `docs/index.html`. Quítalo si además quieres regenerar el panel en
local para revisarlo antes de subirlo.

Si además queréis que corra solo en vuestro propio ordenador (en vez de
GitHub Actions):

**macOS/Linux (cron)** — que corra todos los lunes a las 9:00:
```bash
crontab -e
```
Añade:
```
0 9 * * 1 cd /ruta/a/licitaciones-deportivas && venv/bin/python monitor_licitaciones.py --config config.yaml >> log.txt 2>&1
```

**Windows (Task Scheduler)**: crea una tarea semanal que ejecute
`venv\Scripts\python.exe monitor_licitaciones.py --config config.yaml` con
"Iniciar en" apuntando a la carpeta del proyecto.

Coste: 0€ si usáis un ordenador que ya tenéis encendido, o 2-5€/mes si
preferís un VPS pequeño (DigitalOcean, Hetzner...) para que no dependa de
vuestro portátil.

## 6. Cobertura y límites

- El feed de PLACSP cubre Administración General del Estado y muchos
  ayuntamientos/diputaciones, pero **no todo**: Cataluña, Euskadi y Andalucía
  tienen plataformas propias que conviene revisar aparte si trabajáis mucho
  con esas comunidades.
- Complemento gratuito recomendado: registraos como operador económico en
  [PLACSP](https://contrataciondelestado.es) para tener alertas oficiales de
  respaldo por CPV.
- Las primeras 2-3 semanas, revisad qué os está pasando el filtro y ajustad
  `incluir_palabras_clave` / `excluir_palabras_clave` en `config.yaml` (o
  `config.example.yaml` si usáis GitHub Actions, y luego commiteáis el
  cambio).

# BloodRaven · SERTRACEN Panamá

Monitor multisucursal con dashboard oscuro, historial SQLite y consultas desde Telegram. **Versión 0.2.0-beta.1**, piloto para Ubuntu Server con Docker Compose.

## Estado de esta versión

- Portal SERTRACEN con login propio antes del dashboard, cierre de sesión y cambio de contraseña.
- Inventario web: registrar, editar, pausar y archivar switches Cisco, Dell y UniFi por sucursal; credenciales SNMP cifradas.
- Cuentas de administrador y consulta, controles CSRF, sesiones revocables y auditoría de accesos/cambios. Ver [controles y límites de seguridad](docs/security.md).

- Dashboard conectado al backend: inventario, estado, CPU, temperatura, tráfico de una interfaz por switch e historial de la última hora.
- Modo **demo** predeterminado: datos sintéticos, claramente identificados; no consulta switches. La conversación de Telegram dentro del panel es un simulador.
- Modo **live**: recolector SNMPv3 de solo lectura, cuatro consultas de equipos concurrentes como máximo, ciclo configurable (30 segundos por defecto).
- Bot real opcional: long polling, consultas privadas de usuarios autorizados, botones y cuatro comandos. No envía alertas automáticas.
- Un único acceso publicado: HTTPS TCP 443. El backend, SQLite y el bot quedan dentro del despliegue.
- Historial y eventos persisten en un volumen. Demo y live utilizan bases diferentes.

**Validación:** pruebas automatizadas locales y revisión de la interfaz. Aún no se han consultado los switches reales ni conectado el bot a una cuenta real. El flujo de GitHub Actions comprueba las pruebas y la construcción Docker. Queda validar el despliegue y la conectividad en el Ubuntu del piloto.

## Inventario confirmado

| Identificador | Equipo | Modelo | Perfil |
|---|---|---|---|
| albrook | SW-ALBROOK | CBS350-48FP-4G | cisco_cbs350 |
| chorrera | SW-CHORRERA | CBS350-48FP-4G | cisco_cbs350 |
| colon | SW-COLON | SG350-52MP | cisco_sg350 |

Este es un inventario parcial de tres switches, no la totalidad estimada de la red. Los nombres y modelos son conocidos; sus IP, firmware e índices de interfaz están pendientes.

## Arranque en Ubuntu

Requisitos: Git, Python 3 y Docker Engine con el plugin Docker Compose. El usuario del servidor necesita acceso de lectura al repositorio privado y permiso para ejecutar Docker. Instalar Docker según su [guía oficial para Ubuntu](https://docs.docker.com/engine/install/ubuntu/).

Clonar el repositorio privado con una cuenta autorizada:

```bash
git clone https://github.com/Chicho507/bloodraven.git
cd bloodraven
python3 scripts/configure.py
docker compose up -d --build
docker compose ps
```

El asistente pide la IPv4 o el nombre DNS de Ubuntu, crea `.env` con una contraseña aleatoria y una clave de cifrado del inventario; copia el inventario local a `config/devices.yml`. Conserva ambos archivos si ya existen. El usuario del panel es `admin`; la contraseña está en `BR_PASSWORD` dentro de `.env`. Esta cuenta se crea solamente en el primer arranque. Después, cambia tu contraseña desde **Mi cuenta** y administra las demás cuentas desde **Usuarios y acceso**. Cambiar `BR_PASSWORD` tras inicializar la base no restablece las cuentas.

Abrir `https://IP_O_NOMBRE_DEL_UBUNTU`. Caddy emite un certificado con su CA interna. Para que los equipos cliente lo acepten, distribuir y confiar en **el certificado público raíz** mediante el procedimiento de certificados de la organización, o configurar un certificado corporativo. Exportación del certificado público:

```bash
mkdir -p certificates
docker compose cp proxy:/data/caddy/pki/authorities/local/root.crt certificates/bloodraven-root.crt
```

La clave privada de la CA permanece en el volumen de Caddy. No eliminar ese volumen durante actualizaciones. Ver [TLS interno de Caddy](https://caddyserver.com/docs/caddyfile/directives/tls).

Si Ubuntu ya tiene un servicio en el puerto 443, integrar BloodRaven con su proxy existente antes de arrancar este proxy; no detener un servicio existente sin revisar su uso.

## Conectar los switches

1. Confirmar conectividad desde Ubuntu hasta las IP de gestión y acceso SNMP UDP 161 a través de las redes de las sucursales.
2. Configurar en los switches un usuario SNMPv3 con autenticación, cifrado y acceso de lectura a los objetos necesarios. Comprobar protocolos compatibles con cada firmware: esta versión acepta SHA256 o SHA, y AES128.
3. Entrar al portal con una cuenta administradora y abrir **Administrar → Equipos de red → Registrar switch**. Completar identificador, nombre, sucursal, fabricante/perfil, modelo, IP o DNS de gestión, firmware, ubicación e índice de interfaz. Las nuevas altas comienzan pausadas. Editar los tres registros Cisco iniciales para completar sus datos.
4. Introducir el usuario y ambas claves SNMPv3 en el formulario; se cifran al guardar y no se vuelven a mostrar. Al editar, dejar los tres campos vacíos conserva las claves. Revisar el perfil y habilitar primero un equipo. Las variables `SNMP_*` del servidor siguen disponibles como alternativa heredada para inventarios sin claves propias. Ajustar `BR_ALLOWED_NETWORKS` a las subredes de gestión permitidas: por defecto admite rangos privados RFC1918. El recolector resuelve DNS y verifica la IP antes de consultar.
5. Cambiar `BR_MODE=live` y recrear el servicio:

```bash
docker compose up -d --force-recreate app
docker compose logs --tail=60 app
```

**No necesitas editar YAML para las altas posteriores.** `config/devices.yml` se importa una sola vez al inicializar `control.sqlite3`; desde entonces el inventario del portal es la fuente de configuración, compartida entre demo y live. Cambiar YAML después no sobreescribe los registros web. Archivar conserva los datos históricos en la base, pero esta beta no incluye restauración ni consulta gráfica de muestras archivadas.

Los perfiles **Dell, UniFi y Cisco estándar** consultan disponibilidad y tráfico IF-MIB. No prometen CPU, temperatura ni PoE: se deben confirmar los modelos exactos y las lecturas disponibles. SNMPv3 debe habilitarse en cada equipo o en UniFi Network según corresponda.

`interface_index` es el `ifIndex` SNMP confirmado de la interfaz a medir; no se supone que coincida con el número físico del puerto. Sin índice, el recolector consulta disponibilidad y, para CBS350, CPU y temperatura; el tráfico queda sin datos. Las tasas requieren dos muestras válidas consecutivas. Se descartan cálculos tras reinicio, discontinuidad, cambio de interfaz o disminución del contador.

Las consultas usan una lista explícita de OIDs. No se hace descubrimiento automático, WALK general, SET ni consultas a tablas PoE. El perfil CBS350 utiliza el promedio de CPU de un minuto y un sensor térmico documentado por Cisco; validar su índice y lectura en cada firmware. Un objeto ausente aparece como N/D.

**Colón / SG350-52MP:** se requiere registrar `firmware` y `firmware_reviewed: true` antes de consultar. El aviso Cisco CVE-2026-20185 afecta las versiones 2.5.9.54 y 2.5.9.55 con al menos dos puertos PoE de 60 W habilitados; determinadas consultas SNMP pueden reiniciar el dispositivo. Revisar las condiciones y la mitigación oficial antes del piloto. Marcar el campo documenta esa revisión, no aplica la mitigación. El perfil SG350 de esta versión obtiene disponibilidad y tráfico; CPU y temperatura permanecen sin mapear. [Aviso Cisco](https://sec.cloudapps.cisco.com/security/center/content/CiscoSecurityAdvisory/cisco-sa-sg350-snmp-dos-GEFZr2Tj).

## Telegram real

Crear el bot con BotFather y guardar el token exclusivamente en `.env`. Definir los **IDs numéricos de usuario** autorizados. Configurar:

```dotenv
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=TOKEN_LOCAL_DEL_BOT
TELEGRAM_ALLOWED_USER_IDS=ID_NUMERICO_DEL_USUARIO
```

Recrear `app` con el comando anterior. Cada usuario autorizado debe abrir el bot en Telegram e iniciar su conversación. Solo se aceptan chats privados del propio usuario, tanto mensajes como botones. El bot requiere salida HTTPS a Telegram; no requiere un nuevo puerto de entrada. Un webhook previo impide usar long polling: debe retirarse por el administrador del bot antes de activarlo aquí.

| Consulta | Resultado |
|---|---|
| `/estado` | Resumen y equipos por sucursal |
| `/sucursal albrook` | Estado, métricas y última lectura válida |
| `/alertas` | Equipos que requieren atención |
| `/historial 24h` | Eventos recientes y eventos todavía abiertos |

El bot lee la misma base que el dashboard. En demo identifica sus respuestas como simuladas, incluso si se activa un bot real. No permite cambios en la configuración de switches. Las respuestas son de mejor esfuerzo: se limitan los reintentos y una respuesta puede perderse ante fallas prolongadas; un reinicio entre el envío y guardar el offset puede duplicar una respuesta. Ver [API de Telegram](https://core.telegram.org/bots/api#getupdates).

## Estados y límites

- **Respondiendo:** sysUpTime pudo leerse. Las métricas adicionales pueden no estar disponibles.
- **Advertencia:** temperatura igual o mayor al umbral configurado para ese equipo.
- **Desactualizado:** primer/segundo fallo de lectura o antigüedad superior a tres intervalos más cinco segundos. Los valores actuales se ocultan.
- **Sin respuesta SNMP:** tres consultas consecutivas fallidas. No significa necesariamente que el switch esté apagado: también puede fallar la ruta, la autenticación o la vista SNMP.
- **Pendiente:** equipo deshabilitado, credenciales ausentes o revisión SG350 pendiente; no se consulta.

Se mide tráfico de una interfaz, no velocidad disponible de internet. Para llamarlo consumo de internet hay que confirmar qué tráfico atraviesa ese enlace. El sistema es un piloto con una única instancia de backend, cuentas locales con permisos de administrador o consulta y disponibilidad observada desde un servidor; no es una solución de alta disponibilidad ni una certificación ISO 27001.

## Persistencia y actualización

`BR_RETENTION_DAYS` conserva muestras durante 7 días por defecto (rango 1–90). Los eventos abiertos se conservan. Para respaldar sin inconsistencias de SQLite, detener temporalmente `app`, respaldar el volumen `measurements` y volver a iniciarlo. Ese volumen contiene las muestras y `control.sqlite3` (cuentas, sesiones, inventario cifrado y auditoría). Conservar también `.env`, el inventario inicial y los volúmenes de Caddy en el respaldo protegido de la organización. La clave `BR_ENCRYPTION_KEY` es necesaria para recuperar las credenciales; si se dejó vacía, conservar `inventory.key` del volumen de datos. No generar una clave nueva sobre una base existente: el arranque detecta la diferencia y se detiene.

```bash
git pull --ff-only
docker compose up -d --build
```

Evitar `docker compose down -v`: elimina los volúmenes de datos y certificados. Las IP, contraseñas, bases de datos y certificados locales están excluidos de Git. El inventario local se monta de solo lectura y debe poder leerlo el usuario del contenedor (el asistente lo crea con modo 0644, sin credenciales); `.env` se crea con modo 0600.

## Desarrollo y pruebas

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

Para desarrollo HTTP local, usar **solo en loopback** `BR_COOKIE_SECURE=false` y `BR_PUBLIC_ORIGIN=http://127.0.0.1:8000`. En el servidor conservar cookies seguras y HTTPS; el origen debe coincidir exactamente con la URL del navegador.

Para ejecución directa, exportar las variables de `.env` al proceso según el mecanismo de tu entorno; Uvicorn no las carga automáticamente. Ejecutar una sola instancia/worker y mantener el enlace de desarrollo en loopback:

```bash
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

## Referencias técnicas

- [OIDs CBS350: CPU, temperatura y estado](https://www.cisco.com/c/en/us/support/docs/smb/switches/Cisco-Business-Switching/kmgmt3636-snmpv3-common-oids-cbs350.html).
- [SNMP en CBS350](https://www.cisco.com/c/en/us/td/docs/switches/lan/csbms/CBS_250_350/Administration-Guide/cbs-350/snmp.html).
- [Ficha Cisco SG350](https://www.cisco.com/c/en/us/products/collateral/switches/small-business-smart-switches/data-sheet-c78-737359.html).
- [Publicación de puertos Docker](https://docs.docker.com/engine/network/port-publishing/).

Beta 0.2: 8 de septiembre de 2026. Ver [cambios](CHANGELOG.md).

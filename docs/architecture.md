# Arquitectura del piloto

```mermaid
flowchart LR
  U[Navegador autorizado] -->|HTTPS 443| C[Caddy]
  C --> A[FastAPI + dashboard]
  A --> D[(SQLite persistente)]
  P[Recolector periódico] -->|SNMPv3 GET UDP 161| S[Switches configurados]
  P --> D
  T[Bot Telegram opcional] -->|HTTPS saliente| TG[Telegram Bot API]
  T --> D
```

El recolector y el bot son tareas asíncronas del proceso del backend. La interfaz funciona aunque nadie tenga una sesión abierta: la recolección depende del servidor. Ejecutar una sola instancia de `app`; varios workers duplicarían los ciclos SNMP y competirían por las actualizaciones del bot.

El dashboard consulta `/api/overview` cada 15 segundos. El recolector genera lecturas cada 30 segundos por defecto. Refrescar el panel no aumenta la frecuencia de muestreo. Cada dato y cada respuesta del bot conserva la hora de su última lectura.

## API

Todas las rutas de datos y los archivos del dashboard requieren una sesión válida por cookie detrás de HTTPS. El login y sus recursos son públicos. Las mutaciones requieren origen exacto, token CSRF y JSON. Las rutas de administración validan el rol en el servidor. `/healthz` es una señal de vida del proceso sin datos de inventario; no certifica que el recolector o Telegram estén conectados.

- `GET /api/overview`: inventario y estado actual, sin IP, configuración SNMP ni secretos.
- `GET /api/devices/{id}/history?hours=1`: muestras de un equipo (hasta 24 horas por consulta).
- `GET /api/events?hours=24`: eventos recientes y abiertos, hasta 500 registros.
- `POST /api/demo/telegram`: formateador del bot sin envío real, solo disponible en demo.

- `GET /api/auth/context`, `POST /api/auth/login`: contexto y acceso con nonce previo.
- `POST /api/auth/logout`, `/api/auth/keepalive`, `/api/auth/password`: cierre, actividad y cambio de contraseña.
- `GET/POST /api/inventory`, `PUT /api/inventory/{id}`, `POST /api/inventory/{id}/archive`: inventario, solo administradores.
- `GET/POST /api/admin/users`, `PUT /api/admin/users/{id}`: cuentas y revocación de sesiones, solo administradores.
- `GET /api/admin/audit`: últimos 500 registros, solo administradores.

## Persistencia

SQLite WAL: estado por equipo, muestras indexadas por equipo/fecha, eventos y metadatos de offset Telegram. Se serializan escrituras mediante un bloqueo del proceso. El archivo de demo está separado del archivo live para impedir mezclar evidencia real con datos sintéticos.

`control.sqlite3` conserva cuentas, hashes de contraseñas, sesiones revocables, inventario y auditoría; se comparte entre modos. El YAML solo inicializa el inventario una vez. Las credenciales propias del switch se cifran con Fernet; la clave está en el entorno o en `inventory.key`, fuera del repositorio. Los cambios web actualizan el inventario en memoria, y una lectura en curso se descarta si cambió la revisión del equipo.

Los contadores SNMP de 64 bits se conservan como enteros. Las tasas se calculan con el tiempo transcurrido entre dos lecturas válidas de la misma interfaz. Reinicios y discontinuidades invalidan el cálculo; los huecos quedan visibles. Una primera muestra no se representa como 0 Mbps.

## Próximas validaciones en campo

1. IP, firmware y rutas desde Ubuntu de cada switch.
2. Usuario SNMPv3 de lectura y algoritmos disponibles por firmware.
3. Lectura CBS350 de CPU/temperatura contrastada con su administración web.
4. Índice y sentido de la interfaz que transporta el tráfico que interesa.
5. Revisión documentada del aviso SG350 antes de habilitar Depósito.
6. Umbrales térmicos propios del modelo y criterios operativos de alertas.
7. Confianza de certificados en los equipos cliente y acceso privado del bot.

Fuera del alcance actual: cambios de configuración en switches, descubrimiento automático, consultas PoE, alertas push, SSO/Active Directory, MFA, restauración de archivos, métricas de CPU/temperatura SG350 y operación redundante.

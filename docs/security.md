# Seguridad de la beta 0.2

BloodRaven es un piloto interno. Estos controles están implementados y probados; no constituyen certificación ISO 27001, evaluación completa OWASP ASVS ni aprobación de las políticas corporativas.

## Acceso y sesiones

- Cuentas locales con roles `admin` y `viewer`. El servidor aplica permisos en cada ruta administrativa. No hay registro público de cuentas.
- Contraseñas de 16 a 128 caracteres; PBKDF2-HMAC-SHA256 con 600 000 iteraciones y sal aleatoria por contraseña. Nunca se guardan en texto plano.
- Token de sesión aleatorio, hash del token en SQLite y cookie `__Host-bloodraven`, HttpOnly, Secure y SameSite=Strict en HTTPS. No se guardan tokens en localStorage.
- Vencimiento a los 30 minutos sin actividad y máximo de 8 horas. El refresco automático de métricas no renueva la sesión. Máximo de cinco sesiones por cuenta.
- Logout revoca la sesión en el servidor. Cambiar contraseña o editar una cuenta revoca sus sesiones. Se impide quitarse el rol administrador o desactivar la propia cuenta desde la administración.
- Cinco fallos por cuenta o treinta por IP en quince minutos limitan nuevos intentos. Las respuestas de credenciales incorrectas son genéricas para cuentas existentes y desconocidas.
- Las mutaciones requieren token CSRF, origen exacto y JSON de hasta 32 KiB. El login utiliza un nonce previo. Los errores de validación no reflejan valores enviados como contraseñas.
- Cabeceras CSP, HSTS en HTTPS, protección contra inclusión en marcos y `nosniff`. Los recursos se sirven localmente. CSP permite estilos inline por las barras del dashboard; los scripts son archivos del mismo origen.

Referencias de diseño: [almacenamiento de contraseñas OWASP](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html), [sesiones](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html) y [CSRF](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html).

## Switches y secretos

Solo administradores pueden consultar IP de gestión y editar inventario. Las credenciales propias se cifran con Fernet antes de almacenarlas y el API no devuelve las claves ni su texto cifrado. La clave maestra se guarda en `BR_ENCRYPTION_KEY` o, como alternativa, en `inventory.key` dentro del volumen. El respaldo necesita base y clave original; el cifrado no protege frente al control total del servidor.

Las consultas usan SNMPv3 authPriv de lectura, SHA256/SHA y AES128 según firmware. El recolector verifica las redes permitidas y fija la IP resuelta durante cada consulta. No hace SET, WALK general ni cambios de configuración. `BR_ALLOWED_NETWORKS` debe limitarse a las redes de gestión reales. Los perfiles estándar Dell/UniFi requieren validar modelos, SNMPv3 y vista IF-MIB; no incluyen mapas de CPU o temperatura.

Referencias: [SNMP en UniFi Network](https://help.ui.com/hc/en-us/articles/33502980942615-SNMP-Monitoring-in-UniFi-Network), [ejemplo Dell N-Series](https://www.dell.com/support/kbdoc/en-us/000133707/how-to-configure-snmpv3-on-dell-emc-networking-n-series-switches). El documento Dell no identifica el modelo de los equipos del piloto.

## Despliegue y recuperación

Conservar HTTPS y `BR_COOKIE_SECURE=true`. `BR_PUBLIC_ORIGIN`, cuando se establece, debe coincidir con esquema, nombre y puerto de la URL que utiliza el personal. El contenedor de la aplicación confía en las cabeceras del proxy porque Compose no publica su puerto: mantenerlo accesible solo desde Caddy. Un despliegue con otro proxy debe restringir también ese acceso. Las cookies sin Secure son únicamente una opción de desarrollo en loopback.

El primer inicio crea la cuenta definida por `BR_USERNAME`/`BR_PASSWORD`. Después las cuentas se administran en la base; modificar estas variables no restablece una contraseña. Crear una segunda cuenta administradora individual para recuperación operativa. No hay recuperación por correo ni inicio de sesión corporativo en esta beta.

Respaldar con `app` detenido el volumen `measurements`, `.env` y los volúmenes Caddy, mediante almacenamiento corporativo protegido. Probar la restauración en un entorno aislado. Perder la clave maestra impide recuperar las claves SNMP cifradas. No publicar `.env`, bases, llaves o inventarios reales en GitHub.

La auditoría registra accesos y cambios, sin contraseñas. El panel muestra los últimos 500 eventos; la base conserva los demás. Es un registro local modificable por el administrador del servidor, sin firma ni envío a un SIEM. Definir retención y exportación corporativa antes de uso prolongado.

## Pendiente para ampliar el piloto

SSO/Active Directory, MFA, recuperación de cuentas, registros centralizados y pruebas independientes de seguridad. La conectividad SNMP real, compatibilidad de cada firmware, política de certificados y autorización corporativa de Telegram se validan en el servidor de destino. Las cuentas Telegram autorizadas son independientes de las cuentas del portal.

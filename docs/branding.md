# Personalización local

La distribución pública usa una identidad genérica y un inventario ficticio. Configura `BR_ORGANIZATION` y `BR_REGION` en `.env` para personalizar las páginas. Los textos se escapan antes de insertarse en HTML.

El logo opcional es `config/branding/logo.png`. Esa carpeta se monta de solo lectura y se excluye de Git y del contexto Docker. El archivo se sirve en el login: debe contener únicamente una imagen de marca destinada a los usuarios del portal. Ningún otro archivo de la carpeta se publica. Sin logo aparece la marca de texto de BloodRaven.

El contenedor necesita permiso de lectura para la carpeta y la imagen. El logo se centra y recorta en una franja; los píxeles blancos se integran con el fondo mediante CSS. El archivo original permanece intacto.

Al actualizar un despliegue anterior, conservar el logo en esta carpeta **antes de descargar la actualización**. Conservar también `.env`, el inventario local y los volúmenes de datos. Las actualizaciones no reemplazan las cuentas ni el inventario ya guardados en la base.

Para acceso a través de NAT/VPN, `BR_HOSTNAME` debe ser la IP o el DNS que utiliza el navegador. Si se define `BR_PUBLIC_ORIGIN`, debe ser la URL HTTPS exacta de acceso. Esta versión admite un solo origen para las operaciones autenticadas. Actualizar ambas opciones al cambiar de dirección y recrear app y proxy. El NAT debe reenviar TCP 443 al proxy; esto se configura en la red corporativa. La CA del certificado debe ser de confianza para los clientes.

No publicar direcciones de despliegue, inventarios reales, logos corporativos, configuraciones locales ni capturas del entorno. Eliminar archivos de la rama actual no elimina el historial, forks o copias descargadas anteriormente.

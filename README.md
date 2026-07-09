# Bot de música

Bot de Discord solo de música. Prefijo: `-`

## Comandos
Usa `.help` dentro de Discord para ver la lista completa con ejemplos.

## Despliegue en Railway
1. Sube estos archivos a un repo nuevo en GitHub (interfaz web, sin necesidad de terminal).
2. En Railway, crea un proyecto nuevo conectado a ese repo.
3. En las variables de entorno de Railway, agrega `DISCORD_TOKEN` con el token de tu bot
   (Developer Portal de Discord → tu aplicación → Bot → Reset Token).
4. En el Developer Portal, activa el intent **MESSAGE CONTENT INTENT**.
5. Invita el bot a tu servidor con permisos de **Connect** y **Speak**.
6. Revisa el log de build en Railway: debe instalar `ffmpeg` (viene de `nixpacks.toml`)
   sin errores.

# El efecto mariposa

¿Por qué nadie sabe si lloverá en diez días? 3.000 atmósferas simuladas a la vez con el modelo de Lorenz (1963), en un video renderizado por completo en CPU y en una versión web interactiva.

![La luz blanca se abre en un espectro cuando las atmósferas divergen](media/divergencia.png)

## Qué muestra

En 1963 Edward Lorenz redujo la convección de la atmósfera a tres ecuaciones:

```
dx/dt = σ (y − x)
dy/dt = x (ρ − z) − y
dz/dt = x y − β z          σ = 10, ρ = 28, β = 8/3
```

El video simula 3.000 copias de ese sistema. Todas arrancan casi en el mismo punto: la diferencia entre la primera y la última es de una millonésima. Cada copia tiene un color.

1. Al principio las 3.000 coinciden y la suma de todos los colores se ve como una sola luz blanca.
2. La diferencia crece de forma exponencial. Cuando alcanza el tamaño de la pantalla, la luz se abre en un espectro: cada color es una atmósfera que tomó otro camino.
3. Al final cada atmósfera está en un lugar distinto del atractor.

### La gráfica de abajo es un pronóstico

Los servicios meteorológicos no simulan una atmósfera sino muchas casi idénticas (pronóstico por conjuntos) y cuentan en cuántas llueve. La franja inferior del video es esa misma gráfica: una línea por atmósfera, con la posición `x` a lo largo del tiempo.

El marcador de la derecha usa una analogía: si un ala del atractor fuera lluvia y la otra sol, ¿cuántas de las 3.000 atmósferas están en cada una? Mientras coinciden marca 100 % y 0 %. Cuando divergen se queda cerca de 50 % y 50 %. Las alas del modelo real corresponden al sentido de giro de la convección, no a lluvia y sol.

El número del pie es la separación máxima entre atmósferas, medida en la simulación en cada fotograma.

![Estado final: cada atmósfera en un punto distinto del atractor](media/final.png)

## Versión interactiva

[`docs/index.html`](docs/index.html) es una página sin dependencias que corre la misma simulación en el navegador con WebGL 2. Tiene una sola perilla: la diferencia inicial, desde una billonésima hasta una décima.

Cada lanzamiento anota cuánto tiempo siguieron juntas las atmósferas. Al probar varias diferencias aparece el resultado de fondo: hacer la medición inicial mil veces más precisa solo compra unas pocas unidades de tiempo más de pronóstico fiable.

La página abre con la mariposa ya separada y viva, y una frase: "Basta un aleteo para que el mañana sea otro". Al empezar, todas las atmósferas colapsan en el punto de partida y la historia se cuenta sola, despacio, hasta que la luz se parte; ahí se entrega el control. Se puede saltar.

Tocar la mariposa da un aleteo: todas las atmósferas colapsan en una sola luz, desde donde esté una de ellas, y vuelven a separarse.

El sonido es opcional y sigue la misma idea que el del video: mientras las atmósferas van juntas suena el tema, y al separarse la melodía improvisa. Además, cada nota se ve: suelta una onda y da un pulso de brillo. Hay una diferencia con el video: cada valor de la perilla tiene su propia canción. Son doce, con tonalidad, progresión de acordes y timbres distintos (piano eléctrico, celesta, kalimba, cristal, arpa y campanas, por síntesis FM). Además, el zoom la hace más brillante o más lejana, la rotación mueve cada ala en el estéreo según dónde se ve y la separación entre atmósferas alarga el eco.

La página está en español y en inglés, con un botón para cambiar.

En teléfono la simulación y la perilla caben en una pantalla. Para que vaya fluida, la gráfica de pronóstico se dibuja en GPU, mientras las atmósferas coinciden solo se dibujan 64 con más peso (se ven idénticas), las estelas usan menos segmentos en su parte vieja y la cantidad de atmósferas baja sola si el equipo no sostiene el ritmo.

Para publicarla con GitHub Pages basta con servir la carpeta `docs/`. El fuente está en `web/efecto-mariposa.html` y `web/build.py` genera la página independiente.

## Cómo funciona el render

- **Integración:** Runge-Kutta de orden 2 con paso fijo de 0,0025, en doble precisión.
- **Estelas sin memoria de pantalla:** cada atmósfera se representa con 400 partículas que siguen exactamente la misma trayectoria numérica, cada una con un retraso distinto. La estela existe en 3D, así que la cámara se puede mover sin que se emborrone.
- **Render por densidad:** 1,2 millones de partículas se proyectan con cámara en perspectiva y se acumulan en un histograma de luz lineal. La profundidad de campo se obtiene desplazando cada partícula según su distancia al plano de foco.
- **Revelado:** exposición automática, curva logarítmica, resplandor, destello anamórfico y viñeta.
- **Arranque en frío y rebobinado:** una primera pasada renderiza el momento de la divergencia y lo coloca al inicio del video. Después la mariposa se rebobina con fotogramas de la propia película, hasta el punto de luz del que parte la historia.
- **Rendimiento:** las partículas se reparten entre 8 procesos y se procesan en bloques de 4.096 que caben en caché. No usa GPU y corre con prioridad baja.

## La música la toca la mariposa

Hay un tema original de 16 notas, compuesto para este proyecto: cada frase abre con un salto de sexta y baja por pasos. `sound.py` hace que lo toque la atmósfera central.

- **El tema, mientras van juntas:** cada vuelta al atractor da la siguiente nota del tema, en el instante en que ocurre. El ritmo lo pone el giro; las notas son compuestas.
- **Mano izquierda:** en lo bajo de cada vuelta suena una nota de acorde quebrado (fundamental, quinta, octava, quinta), con timbre de piano.
- **Un acorde cada cuatro vueltas:** re, si menor, sol y la, con una nota de bajo en cada cambio.
- **El ala decide el lado** en el estéreo, y en la izquierda la melodía se dobla una octava abajo.
- **Al separarse, el tema se disuelve:** la melodía improvisa por pasos sobre la escala pentatónica (sube si la vuelta fue más grande, baja si fue más pequeña) y otras cuatro atmósferas acompañan en voz baja, cada una con sus propias vueltas.
- **Cierre:** vuelve la primera frase del tema, cada vez más lenta.

El tema, los acordes, el colchón, el glissando de arpa que sube hasta el momento de la divergencia y el cierre son composición, no datos. El tema no cita ninguna obra existente.

## Uso

```bash
pip install -r requirements.txt
```

```bash
python butterfly.py --out out/efecto_mariposa.mp4
```

Ese comando renderiza el video, sintetiza el audio y lo mezcla. Con `--lang es,en` salen los dos idiomas en una sola pasada (el segundo se guarda como `efecto_mariposa_en.mp4`). Para probar cambios rápido, fotogramas sueltos a media resolución:

```bash
python butterfly.py --scale 0.5 --stills 0,3.6,9,19
```

| Opción | Qué hace | Valor por defecto |
| --- | --- | --- |
| `--lang` | Idioma del texto: `es`, `en` o `es,en` | `es` |
| `--scale` | Resolución relativa a 1080×1080 | `1.0` |
| `--fps` | Fotogramas por segundo | `60` |
| `--workers` | Procesos de simulación | `8` |
| `--trail` | Longitud de estela en el acto final, en pasos | `100` |
| `--tail` | Segundos del acto final | `10` |
| `--crf` | Calidad de H.264 (menor es mejor) | `16` |
| `--no-audio` | No sintetiza ni mezcla la banda sonora | |
| `--info` | Solo imprime el calendario del video | |

Las fuentes del texto son las de Windows (Sitka, Bahnschrift, Cambria, Consolas); en otros sistemas se usa la fuente por defecto de Pillow.

# El efecto mariposa

3.000 atmósferas simuladas a la vez con el modelo de Lorenz (1963), renderizadas por completo en CPU.

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

Ese es el motivo por el que el pronóstico del tiempo se expresa como probabilidad: los servicios meteorológicos simulan muchas atmósferas casi idénticas (pronóstico por conjuntos) y cuentan en cuántas llueve.

El número de la esquina superior es la separación máxima real entre atmósferas, medida en la simulación en cada fotograma.

![Estado final: cada atmósfera en un punto distinto del atractor](media/final.png)

## Cómo funciona

- **Integración:** Runge-Kutta de orden 2 con paso fijo de 0,0025, en doble precisión.
- **Estelas sin memoria de pantalla:** cada atmósfera se representa con 400 partículas que siguen exactamente la misma trayectoria numérica, cada una con un retraso distinto. La estela existe en 3D, así que se puede mover la cámara sin que se emborrone.
- **Render por densidad:** 1,2 millones de partículas se proyectan con cámara en perspectiva y se acumulan en un histograma de luz lineal. La profundidad de campo se obtiene desplazando cada partícula según su distancia al plano de foco.
- **Revelado:** exposición automática, curva logarítmica, resplandor y viñeta.
- **Rendimiento:** las partículas se reparten entre 8 procesos y se procesan en bloques de 4.096 que caben en caché. No usa GPU y corre con prioridad baja.

## Uso

```bash
pip install -r requirements.txt
```

```bash
python butterfly.py --out out/efecto_mariposa.mp4
```

Fotogramas sueltos a media resolución, para probar cambios rápido:

```bash
python butterfly.py --scale 0.5 --stills 7,14.2,26
```

| Opción | Qué hace | Valor por defecto |
| --- | --- | --- |
| `--lang` | Idioma del texto (`es` o `en`) | `es` |
| `--scale` | Resolución relativa a 1080×1080 | `1.0` |
| `--fps` | Fotogramas por segundo | `60` |
| `--workers` | Procesos de simulación | `8` |
| `--trail` | Longitud de estela en el acto final, en pasos | `100` |
| `--tail` | Segundos del acto final | `10` |
| `--crf` | Calidad de H.264 (menor es mejor) | `16` |
| `--info` | Solo imprime el calendario del video | |

Las fuentes del texto son las de Windows (Segoe UI, Cambria, Consolas); en otros sistemas se usa la fuente por defecto de Pillow.

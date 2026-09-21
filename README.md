# 🚀 SmartBancs API

> Microservicio transaccional de alta concurrencia y procesamiento asíncrono, diseñado con un enfoque robusto en la integridad de datos (ACID) y arquitectura desacoplada para IA.

---

## 🛠️ Stack Tecnológico
* **Backend:** Python 3.11, FastAPI, Uvicorn
* **Base de Datos & Concurrencia:** PostgreSQL 15 (Alpine), SQLAlchemy / SQL Puro con control de bloqueos de fila (`SELECT ... FOR UPDATE`)
* **Procesamiento de Datos (ETL):** Python (Pandas / Native scripts) para limpieza y estandarización de transacciones
* **Infraestructura:** Docker y Docker Compose (Contenedorización completa)

---

## 🏛️ Decisiones de Arquitectura y Retos Técnicos

### 1. Integridad Transaccional y Control de Concurrencia (ACID)
En los sistemas bancarios modernos, el mayor riesgo es el fenómeno de *Race Conditions* (condiciones de carrera) durante transferencias simultáneas. SmartBancs resuelve esto mediante:
* **Bloqueos Explícitos de Fila:** Implementación de consultas protegidas con `SELECT ... FOR UPDATE` ordenadas alfabéticamente para prevenir interbloqueos (*deadlocks*).
* **Rollback Automático:** Si una transacción falla por fondos insuficientes o errores de red, la base de datos revierte los cambios de inmediato garantizando que ningún fondo desaparezca.

### 2. IA Desacoplada y No Bloqueante (`BackgroundTasks`)
Para cumplir con estrictos estándares de latencia, el análisis de patrones de fraude o comportamiento financiero impulsado por IA se ejecuta de manera asíncrona:
* Utiliza las `BackgroundTasks` nativas de FastAPI para liberar al cliente en milisegundos (`latency_ms < 30ms`).
* El motor de IA procesa en segundo plano sin acoplarse al hilo principal de la petición HTTP.

### 3. Pipeline de Datos (ETL)
El repositorio incluye un módulo dedicado a la ingesta y depuración de transacciones masivas provenientes de fuentes heterogéneas (`raw_transactions.csv`), transformándolas en estructuras limpias y listas para modelos analíticos.

---

## ⚙️ Guía de Instalación y Ejecución Local

No necesitas instalar dependencias de Python ni configurar bases de datos locales en tu máquina. Todo corre mediante contenedores aislados.

### Prerrequisitos
* [Docker Desktop](https://www.docker.com/products/docker-desktop/) instalado y activo (con soporte WSL2 en Windows).

### Pasos para levantar el entorno:

1. **Clonar o abrir el repositorio en tu terminal:**
   ```bash
   git clone <tu-repositorio>
   cd smartbancs-app
   ---

## 👤 Autor
Desarrollado como parte de soluciones backend de alta eficiencia orientadas a la industria financiera y tecnológica.
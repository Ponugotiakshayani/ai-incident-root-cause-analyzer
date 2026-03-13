# AI Incident Root Cause Analyzer

An observability-driven system that automatically analyzes service failures and identifies the most likely root cause using telemetry signals from a distributed microservice environment.

This project simulates a production-like monitoring system where multiple services expose metrics, Prometheus collects telemetry, and an RCA (Root Cause Analysis) engine analyzes those signals to explain why an incident occurred.

The system also provides real-time visualization through Grafana dashboards.

## System Architecture
Service A  →  Service B
     ↓            ↓
 Prometheus Metrics Collection
            ↓
     RCA Analysis Service
            ↓
       Grafana Dashboard

## Components:

Service A – Upstream microservice that calls Service B

Service B – Downstream dependency that can simulate slow responses or failures

Prometheus – Collects latency and error metrics

RCA Service – Analyzes telemetry signals to determine likely root cause

Grafana – Visualizes service health and incidents

## Tech Stack

Python

FastAPI

Docker

Prometheus

Grafana

Prometheus Client Libraries

## Project Features
Microservice Simulation

Two services simulate a dependency chain:

Service A → calls Service B

Latency and errors propagate downstream

## Metrics Collection

Services expose Prometheus metrics including:

Service latency

Downstream latency

Error rate signals

## Automated Root Cause Analysis

The RCA engine analyzes telemetry signals and determines the most likely cause of incidents.

## Example signals used:

High upstream latency

Downstream latency spikes

Service error rate

Combined correlation of metrics

## The RCA service returns structured analysis including:

Incident type

Root cause

Observations

Likely causes

Recommended next steps

Confidence level

## Real-Time Observability

Grafana dashboards visualize:

Service latency

Downstream latency

Error rates

Incident activity

## Repository Structure
ai-incident-root-cause-analyzer

services
│
├── service-a
│   ├── app
│   │   └── main.py
│   ├── Dockerfile
│   └── requirements.txt
│
├── service-b
│   ├── app
│   │   ├── main.py
│   │   └── ai_rca.py
│   ├── Dockerfile
│   └── requirements.txt
│
├── rca-service
│   ├── app
│   │   ├── main.py
│   │   └── prometheus_client.py
│   ├── Dockerfile
│   └── requirements.txt
│
infra
│
├── prometheus
│   ├── prometheus.yml
│   └── alert.rules.yml
│
├── alertmanager
│   └── alertmanager.yml
│
docker-compose.yml
README.md

## Running the Project

1. Clone the Repository
git clone https://github.com/Ponugotiakshayani/ai-incident-root-cause-analyzer.git
cd ai-incident-root-cause-analyzer

2. Start the System
docker-compose up --build

This will start:

Service A

Service B

RCA Analyzer

Prometheus

Grafana

## Service Endpoints
Service	URL
Service A	   http://localhost:8001

Service B	   http://localhost:8000

RCA Analyzer	http://localhost:8002

Prometheus	http://localhost:9090

Grafana	     http://localhost:3000


## Simulating Failures
Simulate Service Error
http://localhost:8001/error
Simulate Slow Service
http://localhost:8001/slow
Running Root Cause Analysis

After triggering traffic or failures:

http://localhost:8002/analyze

Example output:

{
 "incident": "service errors detected",
 "root_cause": "service-b returning errors",
 "confidence": "high",
 "observations": [
   "service-a latency spike",
   "service-b error rate detected"
 ],
 "recommended_next_steps": [
   "Check service-b logs",
   "Inspect downstream dependencies",
   "Verify error handling paths"
 ]
}

## Grafana Dashboard

Grafana dashboards visualize system metrics including:

Service latency

Downstream latency

Error rates

Incident activity

Login:

http://localhost:3000

Default credentials:

admin / admin
Example Use Case

When Service B begins returning errors:

Prometheus records increased error rate.

Service A latency increases due to downstream failures.

RCA engine analyzes both signals.

The analyzer determines that Service B is the most likely root cause.

Grafana dashboards visualize the event in real time.

## Learning Goals of the Project

This project demonstrates concepts commonly used in modern distributed systems:

Microservice observability

Metrics-driven incident detection

Automated root cause reasoning

Telemetry correlation

Monitoring dashboards

Production-style system architecture

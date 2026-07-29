# CISUC Chatbot
## Technical Documentation

> Version 1.0

---

# Table of Contents

1. Introduction
2. Project Overview
3. System Architecture
4. Knowledge Pipeline
5. Retrieval Architecture
6. Repository Organization and Development Workflow
7. Continuous Integration and Continuous Delivery
8. Deployment Architecture
9. Local Development, Configuration and Testing
10. Future Work

---

# 1. Introduction

## 1.1 Purpose

The CISUC Chatbot is a Retrieval-Augmented Generation (RAG) platform developed for the Centre for Informatics and Systems of the University of Coimbra (CISUC). Its primary objective is to provide users with a conversational interface capable of answering questions using institutional knowledge collected from multiple information sources.

Unlike traditional search systems, which require users to manually browse web pages or documents, the chatbot enables natural-language interaction while grounding its responses on information retrieved from an indexed knowledge base. This approach combines modern Large Language Models (LLMs) with information retrieval techniques to improve the accuracy, relevance, and reliability of generated answers.

The platform extends beyond the chatbot application itself. It includes a complete preprocessing pipeline responsible for collecting and preparing institutional data, a hybrid retrieval system that combines lexical and semantic search, and a fully automated Continuous Integration and Continuous Delivery (CI/CD) pipeline responsible for building, validating, and deploying the application.

This document provides a comprehensive technical description of the platform, including its architecture, preprocessing workflow, retrieval strategy, deployment infrastructure, development workflow, and future evolution.

---

## 1.2 Scope

This document is intended for developers, researchers, maintainers, and system administrators involved in the development or operation of the CISUC Chatbot.

It describes:

- the overall system architecture;
- the preprocessing pipeline;
- the retrieval process;
- the runtime application;
- the repository organization;
- the development workflow;
- the CI/CD pipeline;
- the deployment architecture;
- local development procedures;
- future improvements.

User documentation and operational procedures outside the software platform are intentionally outside the scope of this document.

---

## 1.3 System Overview

The CISUC Chatbot consists of two major subsystems:

1. **Knowledge Preparation**, responsible for collecting, processing, enriching, and indexing institutional information.

2. **Runtime Application**, responsible for retrieving relevant information from the indexed knowledge base and generating responses using a Large Language Model.

These two subsystems operate independently.

The preprocessing pipeline executes only when the knowledge base needs to be updated, while the runtime application continuously serves user requests using the previously generated indices.

This separation significantly reduces response latency by ensuring that computationally expensive preprocessing operations are not executed during user interactions.

The relationship between these subsystems is illustrated below.

```text
                CISUC Information Sources
                           │
                           ▼
                 Preprocessing Pipeline
                           │
          ┌────────────────┴────────────────┐
          ▼                                 ▼
      BM25 Index                     ChromaDB
          │                                 │
          └────────────────┬────────────────┘
                           ▼
                    Runtime Application
                           │
                    Large Language Model
                           │
                           ▼
                     User Response
```

---

# 2. Project Overview

## 2.1 Objectives

The primary objective of the CISUC Chatbot is to improve access to institutional information by allowing users to search and explore CISUC content through natural-language conversations.

Instead of navigating multiple websites, project pages, researcher profiles, or news articles, users can submit questions directly to the chatbot. The platform retrieves relevant information from the institutional knowledge base and uses that information to generate a coherent response.

Typical questions supported by the system include:

- Which researchers work on a specific research topic?
- Which projects are associated with a given research group?
- What publications are related to a particular area?
- What recent news has been published by CISUC?
- Which research groups focus on artificial intelligence?

The objective is not to replace traditional search mechanisms but rather to provide a more intuitive interface that combines retrieval and language generation.

---

## 2.2 Retrieval-Augmented Generation

The CISUC Chatbot follows the Retrieval-Augmented Generation (RAG) paradigm.

Instead of relying exclusively on the internal knowledge of a language model, the system first retrieves relevant information from its indexed knowledge base and only then generates a response.

The complete request lifecycle can be summarized as follows.

```text
User Question
      │
      ▼
Query Processing
      │
      ▼
Hybrid Retrieval
      │
      ▼
Relevant Context
      │
      ▼
Prompt Construction
      │
      ▼
Large Language Model
      │
      ▼
Generated Response
```

This architecture provides several advantages over directly querying a language model.

First, responses are grounded in institutional information collected during preprocessing, reducing the likelihood of hallucinations and improving factual consistency.

Second, updates to the CISUC knowledge base become available after the preprocessing pipeline executes, without requiring changes to the language model itself.

Finally, the retrieval layer remains independent of the language model provider, allowing the system to support different inference backends with minimal changes to the application.

---

## 2.3 Main Components

The platform is organized into four runtime services and one offline preprocessing pipeline.

### GUI

The graphical user interface provides the browser-based chat application. It is responsible for collecting user questions, displaying streamed responses, rendering Markdown content, and presenting deployment metadata.

### Orchestrator

The Orchestrator coordinates the complete request lifecycle. It receives requests from the GUI, retrieves contextual information from the RAG service, constructs prompts, communicates with the selected language model, and streams generated responses back to the client.

### RAG API

The Retrieval-Augmented Generation service performs hybrid retrieval by combining lexical search with semantic vector search. It ranks retrieved information and returns the most relevant context to the Orchestrator.

### ChromaDB

ChromaDB stores the vector embeddings generated during preprocessing. These embeddings enable semantic retrieval based on conceptual similarity rather than exact keyword matching.

### Preprocessing Pipeline

The preprocessing subsystem prepares the knowledge base used by the runtime application. It performs data ingestion, semantic enhancement, structural chunking, embedding generation, and index creation before the chatbot becomes available.

---

## 2.4 Core Technologies

The platform combines modern web technologies with information retrieval and containerized deployment.

| Technology | Purpose |
|------------|---------|
| React | Frontend user interface |
| TypeScript | Typed frontend development |
| Vite | Frontend build system |
| Python | Backend services and preprocessing |
| ChromaDB | Vector database |
| BM25 | Lexical retrieval |
| Docker | Containerization |
| Docker Compose | Service orchestration |
| GitHub Actions | CI/CD automation |
| GitHub Container Registry | Container registry |
| Traefik | Preview routing |
| Nginx | Staging and production reverse proxy |
| OpenAI | Hosted language models |
| Ollama | Self-hosted language models |

Each technology was selected according to its role within the overall architecture, allowing the platform to remain modular while supporting reproducible deployments across different environments.

---

## 2.5 Design Principles

The architecture of the CISUC Chatbot is guided by several fundamental design principles.

### Separation of Concerns

Each major responsibility is implemented by an independent component. The preprocessing pipeline, retrieval service, orchestration layer, frontend application, and deployment infrastructure are developed independently while communicating through clearly defined interfaces.

### Modularity

Individual services can evolve without requiring changes to unrelated components. For example, replacing the language model provider does not require modifications to the retrieval service, while improvements to retrieval do not affect the graphical interface.

### Reproducibility

Application services are distributed as Docker images built by the CI/CD pipeline. Each image is identified by an immutable Git commit SHA, allowing every deployment to be traced back to the exact source code revision from which it was produced.

### Maintainability

The repository organization, reusable scripts, and automated deployment pipeline reduce operational complexity while simplifying future development.

### Extensibility

The modular architecture enables future support for additional data sources, retrieval algorithms, language model providers, evaluation frameworks, and deployment strategies without requiring fundamental architectural changes.

# 3. System Architecture

The CISUC Chatbot adopts a modular, service-oriented architecture that separates data preparation, information retrieval, orchestration, and user interaction into independent components. This separation of responsibilities improves maintainability, simplifies deployment, and enables each subsystem to evolve independently without affecting the remainder of the platform.

At runtime, the application consists of four primary services:

- GUI
- Orchestrator
- RAG API
- ChromaDB

These services interact with an external Large Language Model (LLM), which may be provided either by OpenAI or by a locally hosted Ollama instance.

The preprocessing pipeline, described in the following chapter, operates independently from the runtime application. Its responsibility is to prepare the knowledge base used during retrieval, allowing user requests to be answered without performing expensive preprocessing operations.

The overall architecture is illustrated below.

```text
                    User Browser
                          │
                          ▼
               Reverse Proxy (Nginx / Traefik)
                          │
          ┌───────────────┴───────────────┐
          ▼                               ▼
     React GUI                    Orchestrator
                                          │
                      ┌───────────────────┴───────────────────┐
                      ▼                                       ▼
                  RAG API                           OpenAI / Ollama
                      │
             ┌────────┴────────┐
             ▼                 ▼
        BM25 Index         ChromaDB
```

Every component performs a well-defined task within the request lifecycle. Communication between services is intentionally limited to clearly defined interfaces, reducing coupling and making individual components easier to maintain.

---

## 3.1 Runtime Components

### Graphical User Interface

The graphical user interface provides the primary interaction point between users and the chatbot.

Implemented using React, TypeScript, and Vite, it is responsible for presenting the chat interface while delegating all application logic to the backend services.

Its responsibilities include:

- collecting user questions;
- sending requests to the Orchestrator;
- displaying streamed responses;
- rendering Markdown content;
- presenting loading and error states;
- displaying deployment metadata.

The GUI never communicates directly with the retrieval service or the vector database. All requests are routed through the Orchestrator, ensuring that business logic remains centralized within the backend.

---

### Orchestrator

The Orchestrator is the central coordination component of the platform.

Every request submitted by the user passes through this service before reaching either the retrieval system or the language model.

Its primary responsibilities include:

- validating incoming requests;
- preprocessing user queries;
- requesting contextual information from the RAG API;
- constructing prompts for the language model;
- communicating with the configured LLM provider;
- streaming generated responses back to the client;
- handling runtime errors.

Because the Orchestrator encapsulates all communication with the language model, neither the GUI nor the retrieval service needs to be aware of the selected provider.

---

### RAG API

The Retrieval-Augmented Generation (RAG) service is responsible for retrieving the information required to answer user questions.

Rather than generating responses itself, it searches the indexed knowledge base using hybrid retrieval techniques and returns the most relevant document chunks to the Orchestrator.

Its responsibilities include:

- lexical retrieval using BM25;
- semantic retrieval using ChromaDB;
- ranking retrieved documents;
- Reciprocal Rank Fusion;
- context selection.

The retrieval algorithms are discussed in detail in Chapter 5.

---

### ChromaDB

ChromaDB stores the vector embeddings generated during preprocessing.

Each indexed document chunk is represented by an embedding together with metadata describing its origin within the knowledge base.

When the RAG API performs semantic retrieval, it compares the embedding generated from the user's query with the embeddings stored in ChromaDB, returning the most similar document chunks.

Persistent storage is used to ensure that the indexed knowledge base survives container restarts and application deployments.

---

## 3.2 Request Lifecycle

Every user interaction follows the same execution sequence.

```text
User
 │
 ▼
GUI
 │
 ▼
Orchestrator
 │
 ▼
RAG API
 │
 ├── BM25 Search
 └── Vector Search
        │
        ▼
Retrieved Context
        │
        ▼
Prompt Construction
        │
        ▼
Large Language Model
        │
        ▼
Streamed Response
        │
        ▼
GUI
```

When the user submits a question, the GUI forwards the request to the Orchestrator.

The Orchestrator requests relevant contextual information from the RAG API, which performs both lexical and semantic retrieval over the indexed knowledge base. After ranking the retrieved results, the selected document chunks are returned to the Orchestrator.

The Orchestrator then combines the retrieved context with the user's question to construct the prompt sent to the configured language model. Once generation begins, the resulting tokens are streamed back to the GUI, allowing the user to view the response progressively instead of waiting for the complete answer.

Separating retrieval from generation ensures that responses remain grounded in institutional information while allowing the language model to produce coherent natural-language answers.

---

## 3.3 Runtime Communication

The platform adopts a layered communication model in which each service communicates only with the components required to perform its responsibilities.

```text
Browser
   │
   ▼
GUI
   │
HTTP
   │
   ▼
Orchestrator
   │
HTTP
   │
   ▼
RAG API
   │
   ├────────► BM25
   └────────► ChromaDB
                 │
                 ▼
        Retrieved Context
                 │
                 ▼
          OpenAI / Ollama
                 │
                 ▼
           Streamed Answer
```

This architecture minimizes direct dependencies between services.

For example:

- the GUI never communicates directly with ChromaDB;
- the retrieval service never interacts with the browser;
- the language model has no direct access to the indexed knowledge base;
- preprocessing components are completely isolated from runtime services.

By enforcing these boundaries, the platform remains easier to maintain, test, and extend.

---

## 3.4 Containerization

Each runtime service is packaged as an independent Docker image.

The main application images are:

| Image | Responsibility |
|--------|----------------|
| `cisuc-gui` | Graphical user interface |
| `cisuc-orchestrator` | Request orchestration and LLM communication |
| `cisuc-rag` | Hybrid retrieval service |
| `cisuc-chromadb` | Vector database |

Containerization provides several advantages.

First, it guarantees consistent execution environments across local development, preview deployments, staging, and production.

Second, services can be updated independently without affecting unrelated components.

Finally, the CI/CD pipeline can build, version, and deploy the same images across every supported environment, improving reproducibility and simplifying operational management.

---

## 3.5 Build Metadata

To improve deployment traceability, build information is embedded into the frontend during the application build.

The CI/CD pipeline injects two values into the GUI:

- the deployment environment;
- the Git commit SHA.

These values are displayed within the application, for example:

```text
Staging • a1b2c3d
```

or

```text
Production • e4f5g6h
```

Displaying build metadata allows developers and administrators to immediately identify the version currently running without inspecting deployment logs or container metadata. Since each Docker image is tagged using the corresponding Git commit SHA, this information establishes a direct link between the running application, the source code revision, and the CI/CD workflow that produced the deployment.

# 4. Knowledge Pipeline

The quality of a Retrieval-Augmented Generation (RAG) system depends directly on the quality of its knowledge base. Rather than retrieving information directly from live data sources at runtime, the CISUC Chatbot prepares its knowledge base through an offline preprocessing pipeline that transforms heterogeneous institutional information into a structured, searchable, and semantically enriched collection of documents.

Separating preprocessing from the runtime application provides several advantages. Computationally expensive operations—such as web scraping, semantic enrichment, document chunking, and embedding generation—are performed only when the knowledge base is updated, rather than for every user request. Consequently, the runtime application can focus exclusively on retrieval and response generation, resulting in lower response latency and more predictable performance.

The preprocessing pipeline consists of five sequential stages:

```text
build-preprocessing.sh
        │
        ▼
run-ingestion.sh
        │
        ▼
run-enhancement.sh
        │
        ▼
run-embeddings.sh
        │
        ▼
finish-preprocessing.sh
```

Each stage produces artifacts consumed by the following stage, allowing the pipeline to be executed either as a complete workflow or as individual steps during development and debugging.

---

## 4.1 Pipeline Overview

The objective of the preprocessing pipeline is to convert information originating from multiple CISUC sources into a hybrid retrieval index suitable for both lexical and semantic search.

The complete workflow is illustrated below.

```text
            CISUC Information Sources
                       │
                       ▼
               Data Ingestion
                       │
                       ▼
            Semantic Enhancement
                       │
                       ▼
             Structural Chunking
                       │
                       ▼
            Embedding Generation
                       │
          ┌────────────┴────────────┐
          ▼                         ▼
      BM25 Index               ChromaDB
```

Each stage progressively enriches the collected information until it becomes suitable for efficient retrieval by the runtime application.

The resulting artifacts form the chatbot's knowledge base and are consumed exclusively by the retrieval layer.

---

## 4.2 Building the Preprocessing Environment

Before executing any preprocessing stage, the required execution environment must be prepared.

This is accomplished using:

```bash
./scripts/build-preprocessing.sh
```

The script builds the Docker images required by the preprocessing pipeline and prepares the execution environment used by the remaining stages.

Separating preprocessing from the runtime application ensures that indexing dependencies remain isolated from the chatbot services themselves. It also guarantees reproducibility by executing every preprocessing run within the same containerized environment.

---

## 4.3 Data Ingestion

The ingestion stage is responsible for collecting information from the supported CISUC sources and transforming it into a normalized intermediate representation.

The complete ingestion process can be executed using:

```bash
./scripts/run-ingestion.sh
```

During development, individual ingestion modules can be executed independently:

```bash
./scripts/run-ingestion.sh --source api
```

```bash
./scripts/run-ingestion.sh --source static
```

```bash
./scripts/run-ingestion.sh --source news
```

Executing a single source is particularly useful while developing or debugging an ingestion module, as it avoids unnecessarily processing unrelated sources.

Regardless of the origin of the information, the objective of the ingestion stage is always the same: transform heterogeneous content into a consistent representation that can be processed uniformly by the subsequent stages.

Typical ingestion operations include:

- retrieving static website content;
- consuming structured APIs;
- processing dynamically generated pages;
- extracting relevant metadata;
- removing duplicated or irrelevant content;
- normalizing the collected information.

At the end of this stage, the documents have been cleaned and standardized, but they have not yet been optimized for retrieval.

---

## 4.4 Semantic Enhancement

Although the ingestion stage produces structured documents, the original content often lacks sufficient semantic context for effective retrieval.

The semantic enhancement stage enriches these documents using Large Language Models before they are indexed.

This stage is executed using:

```bash
./scripts/run-enhancement.sh
```

During development, a subset of the dataset can be processed using:

```bash
./scripts/run-enhancement.sh --limit 3
```

Limiting the number of processed documents significantly reduces execution time while validating modifications to the enhancement pipeline.

Depending on the document type, semantic enhancement may include:

- generating concise summaries;
- expanding abbreviations;
- identifying research topics;
- enriching document metadata;
- creating contextual descriptions;
- generating alternative researcher names or aliases.

Rather than replacing the original information, these generated artifacts complement it by making the resulting documents easier to retrieve using natural-language queries.

Since semantic enhancement typically involves calls to external language models, it is one of the most computationally expensive stages of the preprocessing workflow.

---

## 4.5 Structural Chunking

Large documents are not indexed as single units.

Instead, they are divided into smaller chunks while preserving the logical structure of the original content.

Unlike fixed-size chunking strategies, the CISUC Chatbot employs structure-aware chunking. Whenever possible, document boundaries such as headings, sections, or logical divisions are preserved.

For example, a researcher profile may naturally contain independent sections describing:

- biography;
- research interests;
- projects;
- publications;
- contact information.

Preserving these boundaries ensures that each indexed chunk represents a coherent unit of information rather than an arbitrary fragment of text.

To further improve retrieval quality, contextual metadata may be associated with each chunk before embedding generation.

A simplified example is shown below.

```text
Research Group:
Adaptive Computing Systems

Section:
Projects

Content:
...
```

Providing structural context alongside the chunk content reduces ambiguity during retrieval and improves the quality of the information ultimately supplied to the language model.

---

## 4.6 Embedding Generation

After semantic enhancement and structural chunking, each document chunk is converted into a numerical vector representation.

Embedding generation is performed using:

```bash
./scripts/run-embeddings.sh
```

Each generated embedding captures the semantic meaning of the corresponding document chunk, enabling similarity search independently of exact word matching.

During this stage, the preprocessing pipeline:

- generates vector embeddings;
- stores embeddings in ChromaDB;
- associates metadata with each embedding;
- prepares lexical indexing data;
- detects duplicate content.

The resulting vector database supports semantic retrieval, while the lexical index supports keyword-based retrieval. Together, these artifacts form the hybrid retrieval database used by the runtime application.

---

## 4.7 Finalizing the Pipeline

Once all preprocessing stages have completed successfully, the pipeline is finalized using:

```bash
./scripts/finish-preprocessing.sh
```

This final stage prepares the generated artifacts for use by the runtime application.

Depending on the execution context, it may validate outputs, organize generated data, or prepare deployment artifacts required by subsequent application builds.

Keeping this step independent simplifies maintenance and allows additional post-processing tasks to be introduced in future versions without affecting the earlier stages of the pipeline.

---

## 4.8 Pipeline Outputs

The preprocessing workflow produces all information required by the retrieval system.

The principal outputs include:

- normalized document representations;
- semantically enriched content;
- structured document chunks;
- vector embeddings;
- lexical search indices;
- ChromaDB collections and metadata.

These artifacts collectively constitute the chatbot's knowledge base.

Once preprocessing has completed successfully, the runtime application no longer needs to access the original CISUC sources. Instead, it performs retrieval exclusively over the prepared indices, significantly reducing runtime latency while ensuring consistent retrieval behaviour.

---

## 4.9 Incremental Execution

Although the preprocessing pipeline is commonly executed as a complete workflow, each stage can also be run independently.

This capability is particularly valuable during development, where modifications often affect only a single stage of the pipeline.

For example:

- changes to ingestion modules only require re-running the ingestion stage;
- modifications to semantic prompts require only the enhancement stage to be executed;
- changes affecting embeddings require regeneration of vector representations without repeating ingestion.

This modular execution model shortens development cycles and reduces computational cost, especially for stages involving language model inference.

As the platform evolves, this modularity also provides a natural foundation for future incremental preprocessing, where only modified or newly discovered documents would be reprocessed rather than rebuilding the entire knowledge base.

# 5. Retrieval Architecture

The primary objective of the retrieval layer is to identify the information most relevant to a user's question and provide it as context to the language model. Rather than relying exclusively on the knowledge already contained within the model, the CISUC Chatbot retrieves information from its indexed knowledge base before generating a response.

This approach follows the principles of Retrieval-Augmented Generation (RAG), where information retrieval and language generation are treated as two distinct stages. The retrieval system is responsible for selecting the most relevant information from the indexed knowledge base, while the language model transforms that information into a coherent natural-language response.

To support different types of queries, the platform employs a **hybrid retrieval strategy** that combines lexical retrieval using BM25 with semantic retrieval using vector embeddings stored in ChromaDB. The results produced by these retrieval methods are merged and ranked before being supplied to the language model.

The complete retrieval workflow is illustrated below.

```text
                User Question
                      │
                      ▼
             Query Processing
                      │
         ┌────────────┴────────────┐
         ▼                         ▼
    BM25 Retrieval          Vector Retrieval
         │                         │
         └────────────┬────────────┘
                      ▼
          Reciprocal Rank Fusion
                      ▼
              Context Selection
                      ▼
            Prompt Construction
                      ▼
            Large Language Model
                      ▼
             Generated Response
```

Separating retrieval from language generation ensures that improvements to retrieval quality automatically benefit every supported language model without requiring changes to the runtime application.

---

## 5.1 Query Processing

Every interaction begins with a natural-language question submitted through the graphical interface.

For example:

> Which CISUC researchers work on cybersecurity?

The question is forwarded to the Orchestrator, which performs any preprocessing required before requesting information from the retrieval service.

Depending on the query, preprocessing may include:

- whitespace normalization;
- punctuation normalization;
- construction of the retrieval request;
- extraction of relevant entities when appropriate.

The processed query is then forwarded to the RAG API, where lexical and semantic retrieval are executed independently.

This preprocessing stage remains intentionally lightweight, as its primary objective is to prepare the query for efficient retrieval rather than to interpret or answer it.

---

## 5.2 Lexical Retrieval

The first retrieval strategy employed by the platform is **BM25**, a probabilistic ranking algorithm widely used in modern information retrieval systems.

BM25 evaluates documents according to the occurrence and frequency of query terms while considering document length. Unlike simple keyword matching, BM25 produces a relevance score that reflects how well each document matches the submitted query.

Lexical retrieval is particularly effective when users employ terminology already present within the indexed documents.

Typical examples include:

- researcher names;
- project names;
- research group names;
- publication titles;
- acronyms;
- technical terminology.

For example, a query containing the exact name of a research project is expected to retrieve the corresponding project description with high confidence because the same identifier exists within the indexed documents.

Since BM25 relies primarily on textual similarity, it performs exceptionally well for entity-centric queries but may be less effective when users describe concepts using different wording than that found in the original documents.

---

## 5.3 Semantic Retrieval

To complement lexical retrieval, the CISUC Chatbot performs semantic search using vector embeddings stored in ChromaDB.

Instead of comparing individual words, semantic retrieval compares numerical vector representations that capture the meaning of document chunks.

This allows the retrieval system to identify conceptually related information even when the wording differs significantly from the indexed documents.

For example, the following questions may retrieve similar content despite sharing few common words:

- Who works on artificial intelligence?
- Which researchers study machine learning?
- Who investigates AI?

Although these queries use different terminology, they refer to closely related concepts and therefore produce similar vector representations.

The semantic retrieval process consists of three main steps:

1. Generate an embedding representing the user's query.
2. Compare that embedding with the vectors stored in ChromaDB.
3. Return the most similar document chunks.

Each retrieved chunk includes both its textual content and the metadata generated during preprocessing, preserving contextual information required during answer generation.

Semantic retrieval significantly improves the platform's ability to answer natural-language questions that cannot be resolved through exact keyword matching alone.

---

## 5.4 Hybrid Retrieval

Neither lexical nor semantic retrieval is sufficient for every type of query.

Lexical retrieval excels at identifying exact entities and technical terminology, whereas semantic retrieval performs better for conceptual questions and paraphrased expressions.

Rather than selecting one approach over the other, the CISUC Chatbot executes both retrieval methods independently and combines their results into a single ranked list.

The hybrid retrieval process is illustrated below.

```text
              User Query
                   │
      ┌────────────┴────────────┐
      ▼                         ▼
 BM25 Retrieval         Vector Retrieval
      │                         │
      └────────────┬────────────┘
                   ▼
            Result Fusion
                   ▼
          Ranked Documents
```

Executing both retrieval strategies provides a more robust search process than relying on either method individually.

Entity-centric questions benefit primarily from lexical retrieval, while broader conceptual questions are more effectively handled through semantic similarity.

By combining both methods, the platform achieves improved retrieval quality across a wider range of user queries.

---

## 5.5 Reciprocal Rank Fusion

The ranked results produced by lexical and semantic retrieval are combined using **Reciprocal Rank Fusion (RRF)**.

Rather than comparing retrieval scores directly—which are often computed using different scales—RRF combines the rankings themselves.

Documents appearing near the top of multiple ranked lists receive higher final rankings than documents appearing only within a single retrieval method.

This approach offers several advantages:

- independence from retrieval score scales;
- robustness across different retrieval algorithms;
- consistent ranking behaviour;
- reduced dependence on any individual retrieval strategy.

Because the fusion algorithm considers relative ranking instead of absolute scores, it remains effective even when the underlying retrieval methods produce fundamentally different scoring distributions.

The fused ranking is returned to the Orchestrator for context selection.

---

## 5.6 Context Selection

Although retrieval may identify many relevant document chunks, only a limited amount of information can be included within the language model prompt.

The retrieval service therefore selects the highest-ranked document chunks according to the configured retrieval parameters.

Each selected chunk contains:

- document content;
- structural metadata;
- source metadata;
- ranking information.

Preserving this contextual information allows the language model to understand not only the retrieved text itself but also the context from which it originated.

This reduces ambiguity between similar pieces of information and contributes to more accurate responses.

---

## 5.7 Prompt Construction

Once the relevant document chunks have been selected, they are returned to the Orchestrator.

The Orchestrator constructs the final prompt supplied to the language model by combining:

- system instructions;
- retrieved contextual information;
- the user's original question.

Conceptually, prompt construction can be represented as:

```text
System Instructions
          │
Retrieved Context
          │
User Question
          │
          ▼
     Final Prompt
          │
          ▼
 Large Language Model
```

Separating prompt construction from retrieval provides two important benefits.

First, improvements to retrieval quality do not require modifications to prompt engineering.

Second, prompt design can evolve independently of the retrieval algorithms, allowing each subsystem to be optimized without introducing unnecessary coupling.

---

## 5.8 Retrieval Characteristics

The hybrid retrieval strategy adopted by the CISUC Chatbot provides a balanced approach to information retrieval.

Lexical retrieval provides high precision when users reference known entities using their exact names or identifiers. Semantic retrieval extends this capability by identifying conceptually related information, allowing users to express questions naturally without requiring knowledge of the terminology used within the indexed documents.

The fusion of these retrieval methods improves the overall robustness of the system while reducing the weaknesses associated with relying exclusively on either lexical or semantic search.

By the time the language model receives the constructed prompt, the retrieval layer has already identified, ranked, and filtered the information most relevant to the user's request. Consequently, the language model can focus exclusively on generating a coherent response based on grounded institutional knowledge rather than attempting to retrieve information independently.

# 6. Repository Organization and Development Workflow

The CISUC Chatbot is maintained as a single repository containing the preprocessing pipeline, runtime application, deployment infrastructure, automation scripts, and supporting documentation. Although the platform comprises multiple independent services, managing them within a unified repository simplifies dependency management, promotes consistent versioning, and enables coordinated development across all components.

The repository is organized to reflect the logical architecture of the system. Runtime services, preprocessing modules, deployment configuration, and automation scripts are clearly separated, allowing developers to quickly locate the code relevant to a particular subsystem while minimizing coupling between unrelated components.

This chapter describes the organization of the repository, the adopted branching strategy, and the development workflow that governs how changes progress from implementation to production deployment.

---

## 6.1 Repository Structure

The repository is organized into directories corresponding to the major functional areas of the platform.

A simplified representation is shown below.

```text
.
├── GUI/
├── Orchestrator/
├── RAG_CISUC/
├── 1_ingestion/
├── 2_enhancement/
├── 3_embeddings/
├── application/
├── config/
├── scripts/
├── tests/
└── .github/
    └── workflows/
```

Although the repository structure may evolve over time, each top-level directory has a well-defined responsibility.

| Directory | Purpose |
|------------|---------|
| `GUI/` | React frontend application |
| `Orchestrator/` | Request orchestration and language model integration |
| `RAG_CISUC/` | Hybrid retrieval service |
| `1_ingestion/` | Data collection from supported sources |
| `2_enhancement/` | Semantic enrichment pipeline |
| `3_embeddings/` | Structural chunking and embedding generation |
| `application/` | Docker Compose files and deployment configuration |
| `config/` | Shared application configuration |
| `scripts/` | Development, preprocessing, and deployment scripts |
| `tests/` | Automated tests |
| `.github/workflows/` | GitHub Actions workflows |

This organization reflects the separation of concerns established in the system architecture. Runtime services remain independent of preprocessing components, while deployment infrastructure and automation scripts are maintained separately from application logic.

---

## 6.2 Branching Strategy

The project follows a Git Flow-inspired branching model consisting of three primary branch types:

- `feature/*`
- `develop`
- `main`

The overall workflow is illustrated below.

```text
feature/*
      │
      ▼
Pull Request
      │
      ▼
develop
      │
      ▼
Pull Request
      │
      ▼
main
```

Each branch has a distinct purpose within the software delivery lifecycle.

### Feature Branches

Development of new functionality takes place in feature branches created from the current `develop` branch.

Examples include:

```text
feature/gui-build-metadata
feature/hybrid-ranking
feature/news-ingestion
feature/preview-routing
```

Each feature branch should implement a single logical change whenever possible. Keeping branches focused simplifies code review, reduces merge conflicts, and makes future maintenance easier.

Feature branches are never deployed directly to staging or production. Instead, they are validated through automatically generated preview environments.

---

### Develop Branch

The `develop` branch acts as the integration branch for the project.

Completed feature branches are merged into `develop` following code review and successful validation.

Every push to this branch automatically triggers the staging deployment pipeline, making the staging environment representative of the latest integrated development version.

---

### Main Branch

The `main` branch contains production-ready code.

Only changes that have already been validated in the staging environment are promoted to `main`.

A successful push to this branch automatically initiates the production deployment workflow.

As a result, the production environment always reflects the current state of the `main` branch.

---

## 6.3 Development Workflow

Development follows a consistent workflow that ensures all changes are reviewed and validated before reaching production.

The complete lifecycle is illustrated below.

```text
Create Feature Branch
        │
        ▼
Implement Changes
        │
        ▼
Commit Changes
        │
        ▼
Push Feature Branch
        │
        ▼
Open Pull Request
        │
        ▼
Preview Deployment
        │
        ▼
Code Review
        │
        ▼
Merge into develop
        │
        ▼
Staging Deployment
        │
        ▼
Validation
        │
        ▼
Merge into main
        │
        ▼
Production Deployment
```

This process introduces multiple validation stages before software reaches production, allowing defects to be identified early while minimizing deployment risk.

---

## 6.4 Pull Request Workflow

Pull Requests are central to the project's collaborative development process.

Whenever a Pull Request targeting `develop` is opened, synchronized, or reopened, GitHub Actions automatically deploys an isolated preview environment.

Each preview deployment is associated with the Pull Request number, allowing multiple feature branches to be tested simultaneously without interfering with one another.

For example:

```text
/preview/42/
/preview/42/chat
```

These preview environments allow reviewers to validate the complete application—including frontend behaviour, backend services, and routing configuration—before approving a merge.

When the Pull Request is closed, a dedicated cleanup workflow automatically removes the associated preview deployment, releasing any allocated resources.

---

## 6.5 Version Traceability

Maintaining traceability between source code, container images, and deployments is a key objective of the development workflow.

Every application image is tagged using the Git commit SHA from which it was built.

For example:

```text
ghcr.io/<owner>/cisuc-gui:8e3c17f...
```

The frontend also displays the deployment environment together with the abbreviated commit identifier.

For example:

```text
Staging • 8e3c17f
```

or

```text
Production • a1b2c3d
```

This metadata establishes a direct relationship between:

- the running application;
- the source code revision;
- the Docker image;
- the CI/CD workflow responsible for the deployment.

Consequently, identifying the origin of a deployed version requires only the information displayed within the application itself.

---

## 6.6 Development Scripts

Common development tasks are encapsulated within reusable shell scripts to provide a consistent developer experience and reduce manual configuration.

The preprocessing pipeline is executed using:

```bash
./scripts/build-preprocessing.sh

./scripts/run-ingestion.sh

./scripts/run-enhancement.sh --limit 3

./scripts/run-embeddings.sh

./scripts/finish-preprocessing.sh
```

The runtime application can be started locally using:

```bash
./scripts/run-application.sh up-build
```

After startup, the application is available at:

```text
http://localhost:8080
```

Preview environments can also be simulated locally.

```bash
./scripts/run-application-preview.sh
```

This starts multiple preview instances that emulate the routing strategy used during Pull Request deployments.

Example endpoints include:

```text
http://localhost:8080/preview/42/

http://localhost:8080/preview/43/
```

Using standardized scripts simplifies onboarding for new contributors while ensuring consistent execution across different development environments.

---

## 6.7 Configuration Management

The repository distinguishes between configuration intended for deployment and configuration used exclusively during local development.

Shared application configuration is maintained within the repository and consumed by Docker Compose and the CI/CD pipeline.

In contrast, files ending with the `.local` suffix are provided solely for debugging and local experimentation.

These files are **not** used during:

- Docker image builds;
- GitHub Actions workflows;
- preview deployments;
- staging deployments;
- production deployments.

Official application builds therefore remain independent of developer-specific configuration, ensuring reproducibility across all supported environments.

Developers should avoid relying on `.local` configuration when implementing functionality intended for deployment.

---

## 6.8 Testing

Although the testing strategy will continue to evolve, the repository already provides a structured location for automated tests and supports validation at multiple levels of the platform.

Typical validation activities include:

- unit tests;
- integration tests;
- frontend compilation;
- static analysis;
- Docker Compose validation.

Depending on the component being modified, developers may execute commands such as:

```bash
python -m pytest
```

```bash
npm run lint
```

```bash
npm run build
```

```bash
docker compose config
```

Executing these checks before opening a Pull Request helps identify issues early and reduces failures during Continuous Integration.

---

## 6.9 Summary

The repository organization and development workflow establish a structured process for implementing, reviewing, validating, and deploying new functionality.

A modular repository layout separates preprocessing, runtime services, deployment infrastructure, and automation, while the branching strategy ensures that changes progress through increasingly stable environments before reaching production.

Together with standardized development scripts and automated preview deployments, this workflow enables collaborative development while maintaining traceability, reproducibility, and deployment reliability.

# 7. Continuous Integration and Continuous Delivery

The CISUC Chatbot employs a Continuous Integration and Continuous Delivery (CI/CD) pipeline to automate the build, validation, publication, and deployment of both the preprocessing pipeline and the runtime application. Implemented using GitHub Actions, the pipeline ensures that every deployment is reproducible, traceable, and derived from a validated source code revision.

Automating these processes reduces manual intervention, minimizes deployment errors, and provides a consistent software delivery workflow across preview, staging, and production environments.

From a high-level perspective, the pipeline distinguishes between two complementary phases:

- **Continuous Integration (CI)**, responsible for building and validating the application.
- **Continuous Delivery (CD)**, responsible for deploying validated artifacts to the appropriate environment.

This separation allows deployment workflows to reuse artifacts generated during the build process while ensuring that only successfully built images are deployed.

---

## 7.1 Pipeline Overview

The CI/CD pipeline is triggered by three types of GitHub events:

- Pull Requests targeting the `develop` branch;
- pushes to the `develop` branch;
- pushes to the `main` branch.

Each event follows a different deployment path while sharing the same build process.

```text
                    GitHub Event
                          │
          ┌───────────────┼────────────────┐
          │               │                │
          ▼               ▼                ▼
     Pull Request    Push develop     Push main
          │               │                │
          ▼               ▼                ▼
   Preprocessing   Preprocessing    Preprocessing
          │               │                │
          ▼               ▼                ▼
     Application     Application      Application
          │               │                │
          ▼               ▼                ▼
 Preview Deploy   Staging Deploy   Production Deploy
```

Regardless of the deployment target, every deployment depends on the successful completion of the application build workflow.

This guarantees that preview, staging, and production environments are always deployed from validated container images.

---

## 7.2 Continuous Integration

The Continuous Integration phase is responsible for producing the deployable artifacts used throughout the remainder of the pipeline.

Whenever the pipeline is triggered, GitHub Actions performs a sequence of automated tasks that includes:

1. Checking out the repository.
2. Preparing the build environment.
3. Authenticating with GitHub Container Registry (GHCR).
4. Building the required Docker images.
5. Tagging each image with the triggering Git commit SHA.
6. Publishing the images to GHCR.

Using immutable commit-based tags ensures that every generated image corresponds to a unique version of the source code.

For example:

```text
ghcr.io/<owner>/cisuc-gui:8e3c17f...
```

These immutable images become the deployment artifacts consumed by the delivery workflows.

---

## 7.3 Preprocessing Workflow

The preprocessing workflow is responsible for generating the artifacts required by the retrieval system before the runtime application is deployed.

Its responsibilities include:

- building preprocessing containers;
- executing data ingestion;
- performing semantic enhancement;
- generating embeddings;
- preparing retrieval artifacts.

The preprocessing workflow is implemented independently from the application build.

This separation reflects the architecture of the platform, where preprocessing and runtime execution represent distinct concerns. As a result, improvements to the preprocessing pipeline can be made without affecting the application build process.

---

## 7.4 Application Workflow

The application workflow builds the runtime services that compose the chatbot.

These services include:

- GUI;
- Orchestrator;
- RAG API;
- ChromaDB.

After building the application images, the workflow publishes them to GitHub Container Registry using immutable Git commit SHA tags.

The build process follows the sequence illustrated below.

```text
Checkout Repository
        │
        ▼
Load Configuration
        │
        ▼
Login to GHCR
        │
        ▼
Build Docker Images
        │
        ▼
Tag Images
        │
        ▼
Publish Images
```

By separating image creation from deployment, the same validated artifacts can be promoted through multiple environments without requiring additional builds.

---

## 7.5 Preview Deployments

One of the distinguishing characteristics of the CI/CD pipeline is its support for automatically generated preview environments.

Whenever a Pull Request targeting the `develop` branch is opened, synchronized, or reopened, GitHub Actions deploys an isolated preview instance of the application.

Each preview environment is associated with the Pull Request number and is accessible through a dedicated route.

For example:

```text
/preview/42/
/preview/42/chat
```

These deployments allow reviewers to validate the complete application—including frontend behaviour, backend services, and routing configuration—before changes are merged into the integration branch.

Multiple preview environments can coexist simultaneously because routing is handled dynamically by Traefik, allowing independent feature branches to be tested in parallel.

---

## 7.6 Preview Cleanup

Preview environments are temporary by design.

When a Pull Request is closed, a dedicated cleanup workflow automatically removes the corresponding deployment.

The cleanup process includes:

- stopping preview containers;
- removing Docker networks;
- removing dynamically created routes;
- releasing allocated resources.

Automating cleanup prevents obsolete preview environments from consuming server resources after development has concluded.

---

## 7.7 Staging Deployment

The staging deployment workflow is triggered automatically whenever changes are merged into the `develop` branch.

Unlike preview environments, which validate individual feature branches, the staging environment represents the latest integrated version of the application.

The deployment sequence is illustrated below.

```text
Push develop
      │
      ▼
Preprocessing
      ▼
Application
      ▼
Staging Deployment
```

The deployment workflow performs the following operations on the remote server:

1. Establish an SSH connection.
2. Clone the repository if it does not already exist.
3. Fetch the latest repository state.
4. Reset the repository to the triggering commit SHA.
5. Authenticate with GitHub Container Registry.
6. Pull the required container images.
7. Start or update the Docker Compose project.
8. Promote the deployed images using the `develop` tag.
9. Remove unused Docker images.

Deploying the repository at the exact triggering commit guarantees that both the deployment scripts and the application correspond to the same source code revision.

---

## 7.8 Production Deployment

The production deployment workflow follows the same overall strategy as the staging deployment but is triggered by pushes to the `main` branch.

```text
Push main
    │
    ▼
Preprocessing
    ▼
Application
    ▼
Production Deployment
```

To minimize deployment risk, production deployments are serialized so that only one deployment may execute at any given time.

Following a successful deployment, the immutable images generated during the build stage are promoted using the `production` tag.

This promotion strategy provides stable references to the latest production images while preserving the original immutable commit identifiers.

---

## 7.9 Immutable Image Versioning

Every runtime image produced by the CI pipeline is tagged using the full Git commit SHA.

For example:

```text
ghcr.io/<owner>/cisuc-gui:8e3c17f6...
```

These immutable tags uniquely identify the exact source code revision from which each image was built.

Following a successful deployment, the same images receive environment-specific aliases.

| Environment | Mutable Tag |
|-------------|-------------|
| Staging | `develop` |
| Production | `production` |

The immutable SHA remains the authoritative identifier for reproducibility, while the mutable tags provide convenient references to the latest successfully deployed versions of each environment.

---

## 7.10 Build Metadata

During the application build, the CI pipeline injects deployment metadata into the frontend.

The embedded metadata consists of:

- the deployment environment;
- the Git commit SHA.

These values are displayed within the application interface.

Examples include:

```text
Staging • a1b2c3d
```

```text
Production • e4f5g6h
```

Displaying build metadata allows developers and system administrators to identify the deployed version immediately, simplifying debugging and deployment verification.

---

## 7.11 Deployment Security

The deployment workflows require several sensitive credentials, including SSH keys, container registry credentials, and language model configuration.

To protect these values:

- deployment secrets are stored as GitHub Secrets;
- environment-specific credentials are maintained independently;
- remote deployments are performed exclusively over SSH;
- secrets are never committed to the repository.

When secrets must be transferred to the deployment server, they are Base64 encoded before transmission and decoded on the remote host.

Although Base64 encoding is not an encryption mechanism, it provides a reliable way to safely transmit multiline values through shell commands executed over encrypted SSH connections.

---

## 7.12 Summary

The CI/CD pipeline provides an automated software delivery process that transforms source code changes into reproducible deployments with minimal manual intervention.

Every deployment follows the same lifecycle: build immutable container images, publish them to GitHub Container Registry, and deploy those validated artifacts to the appropriate environment.

By combining automated builds, preview deployments, staged promotion through development and production environments, immutable image versioning, and embedded build metadata, the pipeline ensures that every deployed version can be traced directly back to the source code revision from which it originated.

The following chapter describes the runtime deployment infrastructure that hosts these artifacts, including Docker Compose, reverse proxy configuration, environment organization, and deployment architecture.

# 8. Deployment Architecture

The deployment architecture is responsible for hosting the CISUC Chatbot across multiple environments while ensuring consistency, reproducibility, and minimal operational overhead. Rather than building the application directly on the deployment server, the infrastructure deploys immutable Docker images previously produced by the Continuous Integration pipeline.

This approach separates software delivery from runtime execution. GitHub Actions is responsible for building and publishing container images, while the deployment infrastructure retrieves those validated artifacts and starts the application using Docker Compose.

The platform currently supports three deployment environments:

- Preview
- Staging
- Production

Although each environment serves a different purpose, they all execute the same application images, differing only in their configuration, routing, and deployment triggers.

The overall deployment architecture is illustrated below.

```text
                     GitHub Container Registry
                                │
                    Immutable Docker Images
                                │
                                ▼
                     Deployment Server (SSH)
                                │
                  ┌─────────────┴─────────────┐
                  ▼                           ▼
            Docker Compose             Reverse Proxy
                  │                           │
      ┌───────────┼───────────┐               │
      ▼           ▼           ▼               ▼
     GUI     Orchestrator    RAG        Browser Requests
                  │
                  ▼
              ChromaDB
```

By separating image creation from deployment, every environment executes software that has already been validated during the CI pipeline.

---

## 8.1 Deployment Environments

The CISUC Chatbot is deployed to three distinct environments, each supporting a different stage of the development lifecycle.

| Environment | Purpose | Trigger |
|-------------|---------|---------|
| Preview | Validation of Pull Requests | Pull Request events |
| Staging | Integration testing | Push to `develop` |
| Production | Public deployment | Push to `main` |

Although the deployment mechanisms are nearly identical, each environment provides a different level of stability.

Preview environments validate individual feature branches.

The staging environment validates the integrated development branch before release.

The production environment hosts the stable version of the application intended for end users.

Using multiple environments allows software to progress through increasingly stable stages before reaching production.

---

## 8.2 Deployment Server

Application deployment is performed on a remote Linux server accessed through SSH.

The deployment workflows connect to the server using a dedicated deployment key configured as a GitHub Actions secret.

Before deployment begins, the workflow ensures that the repository exists on the remote host.

If the repository is not already present, it is cloned automatically.

Subsequent deployments simply fetch the latest repository state before resetting it to the commit corresponding to the deployment.

The repository is maintained in:

```text
$HOME/CISUC_chatbot
```

Resetting the repository to the triggering Git commit ensures that deployment scripts always correspond to the exact version of the application being deployed.

---

## 8.3 Docker Compose

Application services are managed using Docker Compose.

Rather than manually starting individual containers, the deployment workflow delegates service management to a single Compose configuration.

The runtime stack consists of the following services:

```text
GUI
Orchestrator
RAG API
ChromaDB
```

Each service executes independently while communicating through the Docker network created by Docker Compose.

This approach provides several advantages:

- simplified service orchestration;
- automatic networking;
- reproducible deployments;
- independent service updates;
- simplified operational management.

Because Docker Compose defines the complete runtime topology, deployments remain identical across development, staging, and production environments.

---

## 8.4 Reverse Proxy

Incoming HTTP requests are handled by a reverse proxy before reaching the application containers.

The platform uses two reverse proxy solutions depending on the deployment environment.

| Environment | Reverse Proxy |
|-------------|---------------|
| Preview | Traefik |
| Staging | Nginx |
| Production | Nginx |

### Preview Routing

Preview deployments are designed to allow multiple feature branches to coexist on the same server.

Traefik dynamically routes requests according to the Pull Request number.

For example:

```text
/preview/42/
```

and

```text
/preview/43/
```

can execute simultaneously while sharing the same infrastructure.

Because routing is generated dynamically, preview deployments can be created and removed automatically without requiring manual proxy configuration.

---

### Staging and Production Routing

Unlike preview environments, staging and production expose a single stable application instance.

Nginx acts as the public entry point, forwarding incoming requests to the appropriate backend services.

Using Nginx for the permanent environments provides:

- stable routing;
- efficient static file serving;
- HTTP request forwarding;
- simplified operational management.

---

## 8.5 Deployment Process

Although staging and production deployments target different environments, both follow the same execution sequence.

```text
GitHub Actions
        │
        ▼
SSH Connection
        │
        ▼
Repository Synchronization
        │
        ▼
GitHub Container Registry Login
        │
        ▼
Pull Docker Images
        │
        ▼
Docker Compose Up
        │
        ▼
Health Verification
        │
        ▼
Image Promotion
```

After connecting to the deployment server, the workflow synchronizes the repository with the commit corresponding to the deployment.

Next, it authenticates with GitHub Container Registry, allowing Docker Compose to retrieve the required container images.

Finally, the application services are started or updated using the deployment Compose configuration.

Because every deployment executes the same sequence of operations, staging and production remain operationally consistent.

---

## 8.6 Image Promotion

The CI pipeline builds immutable images identified by the Git commit SHA.

For example:

```text
gui:8e3c17f...
rag:8e3c17f...
orchestrator:8e3c17f...
```

Once deployment completes successfully, these immutable images receive additional environment-specific tags.

| Environment | Promoted Tag |
|-------------|--------------|
| Staging | `develop` |
| Production | `production` |

This promotion occurs directly on the deployment server after successful validation.

Importantly, promotion does **not** rebuild the images.

Instead, the deployment server simply assigns additional tags to the existing immutable images before pushing those aliases back to GitHub Container Registry.

Consequently:

- immutable SHA tags always identify the original build;
- mutable tags always identify the latest successful deployment.

This strategy combines reproducibility with operational convenience.

---

## 8.7 Environment Configuration

Each deployment environment uses its own configuration while sharing the same application images.

Examples of environment-specific configuration include:

- API credentials;
- language model configuration;
- deployment environment identifiers;
- container registry credentials.

Sensitive values are managed using GitHub Secrets and injected into the deployment workflow at runtime.

Before transmission to the deployment server, multiline secrets are Base64 encoded and subsequently decoded on the remote host.

Examples include:

- `OPENAI_API_KEY`
- `OLLAMA_URL`
- `GHCR_TOKEN`

Environment-specific configuration allows the same application images to be reused across every deployment target without modification.

---

## 8.8 Deployment Verification

Following deployment, several mechanisms help verify that the expected application version is running.

The frontend displays:

- deployment environment;
- Git commit SHA.

For example:

```text
Production • e4f5g6h
```

This information can be compared directly with:

- GitHub Actions workflow logs;
- Docker image tags;
- Git commit history.

Consequently, determining the source code revision of a running deployment requires no access to the deployment server itself.

---

## 8.9 Operational Characteristics

The deployment architecture was designed with reproducibility and maintainability as primary objectives.

Several architectural decisions contribute to these goals.

First, runtime services are entirely containerized, ensuring consistent execution regardless of the deployment environment.

Second, deployments consume immutable artifacts generated during Continuous Integration rather than rebuilding software on the deployment server.

Third, application configuration remains independent from the application images themselves, allowing the same images to be reused across preview, staging, and production.

Finally, automated deployment workflows eliminate manual deployment procedures, reducing operational complexity while improving deployment consistency.

---

## 8.10 Summary

The deployment architecture provides a reliable mechanism for executing the CISUC Chatbot across preview, staging, and production environments using a common operational model.

GitHub Actions retrieves validated container images from GitHub Container Registry, deploys them through Docker Compose over SSH, and exposes the application through environment-specific reverse proxies.

By combining immutable image versioning, automated deployments, environment-specific configuration, and standardized runtime infrastructure, the platform achieves reproducible deployments while minimizing operational overhead.

The final chapter describes the procedures for local development, testing, and future directions for the continued evolution of the platform.

# 9. Local Development, Configuration and Testing

To facilitate development, debugging, and experimentation, the CISUC Chatbot provides a collection of scripts that automate the setup and execution of both the preprocessing pipeline and the runtime application. These scripts encapsulate the commands required to build containers, execute preprocessing stages, start application services, and emulate deployment environments, allowing developers to work with the platform without manually managing individual Docker commands.

By standardizing common development tasks, the project reduces the differences between local execution and automated deployments while improving reproducibility across development environments.

---

## 9.1 Development Environment

The platform is designed to execute entirely within Docker containers.

Using containerized services ensures that developers work in an environment that closely matches those used for preview, staging, and production deployments, reducing issues caused by operating system differences or locally installed dependencies.

The principal development requirements are:

- Git
- Docker
- Docker Compose

After cloning the repository, developers can execute the provided scripts without installing the individual runtime dependencies of each service.

---

## 9.2 Running the Preprocessing Pipeline

The preprocessing pipeline can be executed either as a complete workflow or stage by stage.

Before running any preprocessing task, the required Docker images should be built.

```bash
./scripts/build-preprocessing.sh
```

Once the preprocessing environment has been prepared, the ingestion stage can be executed.

```bash
./scripts/run-ingestion.sh
```

Alternatively, individual ingestion modules may be executed independently.

```bash
./scripts/run-ingestion.sh --source api
```

```bash
./scripts/run-ingestion.sh --source static
```

```bash
./scripts/run-ingestion.sh --source news
```

Running individual ingestion sources is particularly useful during development, as it avoids unnecessarily processing the complete dataset when only a single ingestion module has been modified.

---

## 9.3 Semantic Enhancement

After ingestion has completed successfully, semantic enrichment can be performed.

The complete enhancement stage is executed using:

```bash
./scripts/run-enhancement.sh
```

For development purposes, a limited number of documents may be processed.

```bash
./scripts/run-enhancement.sh --limit 3
```

Limiting execution to a small subset significantly reduces processing time while validating prompt modifications or changes to the enhancement pipeline.

---

## 9.4 Embedding Generation

Following semantic enhancement, vector embeddings are generated for every processed document.

This stage is executed using:

```bash
./scripts/run-embeddings.sh
```

During execution, document chunks are converted into vector representations suitable for semantic retrieval and stored within the configured ChromaDB instance.

Upon completion, the retrieval database contains both the semantic embeddings and the metadata required by the runtime application.

---

## 9.5 Finalizing the Pipeline

The preprocessing workflow concludes with the finalization stage.

```bash
./scripts/finish-preprocessing.sh
```

This step prepares the generated artifacts for use by the runtime application and completes any remaining post-processing tasks required before the chatbot can execute retrieval operations.

Once this stage has completed successfully, the knowledge base is ready for use.

---

## 9.6 Running the Application

The complete runtime application can be started locally using:

```bash
./scripts/run-application.sh up-build
```

This command builds any required application images before starting the runtime services using Docker Compose.

After startup, the chatbot becomes available at:

```text
http://localhost:8080
```

The runtime stack consists of the same services used in deployment environments:

- GUI
- Orchestrator
- RAG API
- ChromaDB

Consequently, local execution closely resembles the architecture used in staging and production.

---

## 9.7 Preview Environment Simulation

The repository also provides a mechanism for reproducing preview deployments locally.

This can be started using:

```bash
./scripts/run-application-preview.sh
```

The script launches multiple application instances that emulate the routing behaviour used during Pull Request deployments.

Example endpoints include:

```text
http://localhost:8080/preview/42/
```

```text
http://localhost:8080/preview/43/
```

This capability allows developers to validate routing behaviour and preview-specific configuration without requiring a GitHub Actions deployment.

---

## 9.8 Configuration

Application configuration is managed separately from the source code.

Environment-specific values—including API credentials and language model configuration—are supplied through environment variables rather than being embedded within the application itself.

During official builds, configuration is provided by the CI/CD pipeline.

For local development, developers may use local configuration files where appropriate.

However, files ending with the `.local` suffix exist exclusively to simplify debugging and local experimentation.

These files are **not** used during:

- Docker image builds;
- GitHub Actions workflows;
- preview deployments;
- staging deployments;
- production deployments.

Official deployments therefore rely exclusively on repository configuration together with the environment-specific values supplied by GitHub Actions.

Maintaining this separation ensures that developer-specific configuration never becomes part of the deployed application.

---

## 9.9 Testing

The repository supports several forms of validation during development.

Depending on the component being modified, developers may execute tests, static analysis, or build verification before opening a Pull Request.

Typical validation commands include:

```bash
python -m pytest
```

```bash
npm run lint
```

```bash
npm run build
```

```bash
docker compose config
```

Although the current testing strategy continues to evolve, these checks help identify implementation errors before code enters the Continuous Integration pipeline.

Executing validation locally reduces unnecessary CI failures and shortens the feedback cycle during development.

---

## 9.10 Debugging

Because every major component executes within Docker containers, debugging can typically be performed by inspecting service logs.

Common debugging activities include:

- viewing container logs;
- restarting individual services;
- rebuilding modified images;
- executing preprocessing stages independently.

The modular organization of the platform also simplifies troubleshooting by allowing developers to isolate preprocessing, retrieval, orchestration, or frontend behaviour without affecting unrelated components.

Similarly, the ability to execute individual preprocessing stages independently reduces debugging time by avoiding unnecessary recomputation of the entire knowledge pipeline.

---

## 9.11 Development Workflow

A typical local development session follows the sequence illustrated below.

```text
Clone Repository
        │
        ▼
Build Preprocessing Environment
        │
        ▼
Run Preprocessing Pipeline
        │
        ▼
Start Runtime Application
        │
        ▼
Modify Source Code
        │
        ▼
Execute Local Validation
        │
        ▼
Commit Changes
        │
        ▼
Open Pull Request
```

This workflow mirrors the progression followed by the CI/CD pipeline, allowing issues to be identified locally before changes are submitted for review.

---

## 9.12 Summary

The CISUC Chatbot provides a streamlined local development workflow built around reusable scripts and containerized services.

Developers can execute preprocessing stages independently, start the complete runtime application with a single command, emulate preview deployments, and validate their changes before submitting them for review.

By closely matching the execution environment used during automated deployments, the local development process improves reproducibility, reduces configuration errors, and provides a consistent foundation for collaborative software development.

The following and final chapter discusses potential future improvements to the platform, including evaluation, scalability, observability, and enhancements to both the preprocessing pipeline and deployment infrastructure.

# 10. Future Work

The CISUC Chatbot provides a robust foundation for conversational access to institutional information through Retrieval-Augmented Generation. Nevertheless, several opportunities remain to improve the platform's functionality, scalability, maintainability, and operational maturity.

Many of these improvements focus not only on expanding the chatbot's capabilities but also on strengthening the engineering practices surrounding its development and deployment. As the project evolves, future work should address improvements to the preprocessing pipeline, retrieval quality, software testing, deployment reliability, and operational monitoring.

---

## 10.1 Automated Testing

Although the current development workflow includes build validation and manual verification, automated testing should become a more prominent component of the software lifecycle.

Future work should introduce comprehensive test suites covering the different layers of the platform, including:

- preprocessing modules;
- retrieval algorithms;
- orchestration logic;
- frontend components;
- API endpoints;
- deployment scripts.

Automated tests would improve confidence when introducing new features while reducing the likelihood of regressions.

In addition to unit testing, integration tests should validate communication between services, ensuring that changes to one component do not unintentionally affect the behaviour of the overall system.

Integrating these tests into the Continuous Integration pipeline would prevent software that fails validation from progressing toward deployment.

---

## 10.2 Retrieval Evaluation

Evaluating the quality of Retrieval-Augmented Generation systems remains an active area of research.

While the current platform retrieves relevant information using a hybrid search strategy, future versions should include systematic evaluation of retrieval and response quality.

One promising direction is the integration of **Ragas**, a framework specifically designed for evaluating RAG systems.

Such an evaluation framework could measure characteristics including:

- context precision;
- context recall;
- answer faithfulness;
- answer relevance;
- retrieval accuracy.

Rather than relying exclusively on manual testing, these quantitative metrics would provide objective evidence of improvements or regressions following changes to the retrieval pipeline.

---

## 10.3 Quality Gates

Once automated evaluation becomes available, the CI/CD pipeline could incorporate quality gates that validate retrieval performance before deployment.

For example, modifications to retrieval algorithms or preprocessing prompts could automatically trigger benchmark evaluations.

If the resulting system performs significantly worse than the current deployment according to predefined evaluation metrics, deployment could be prevented until the regression is addressed.

Introducing quality gates would extend Continuous Integration beyond software correctness by validating the quality of the chatbot's responses themselves.

---

## 10.4 Incremental Preprocessing

The current preprocessing pipeline rebuilds the knowledge base whenever preprocessing is executed.

Although this approach simplifies implementation, it becomes increasingly expensive as the volume of institutional information grows.

Future versions of the platform should support incremental preprocessing, where only modified or newly discovered documents are processed.

A possible workflow is illustrated below.

```text
Changed Documents
        │
        ▼
Detect Modified Content
        │
        ▼
Reprocess Only Changes
        │
        ▼
Update Indices
```

Incremental execution would substantially reduce preprocessing time while decreasing computational costs associated with semantic enhancement and embedding generation.

---

## 10.5 Improved Document Processing

The current preprocessing pipeline primarily targets structured institutional information.

Future work should expand support for additional document formats, particularly PDF documents containing reports, technical documentation, or scientific publications.

Supporting PDF ingestion would require improvements to several preprocessing stages, including:

- document parsing;
- text extraction;
- layout preservation;
- structural chunking;
- metadata extraction.

Enhancing document processing capabilities would significantly increase the range of information available to the chatbot.

---

## 10.6 Session Persistence

Currently, conversations are handled independently, with limited persistence between user sessions.

Future versions could introduce persistent conversation storage, enabling users to resume previous conversations without losing context.

Possible capabilities include:

- conversation history;
- multi-session continuity;
- user-specific chat histories;
- conversation export.

Persistent sessions would improve the usability of the platform, particularly for users conducting extended exploratory interactions.

---

## 10.7 Observability

As the platform grows, operational visibility becomes increasingly important.

Future work should introduce a comprehensive observability stack capable of monitoring application behaviour across all services.

Relevant monitoring data includes:

- request latency;
- retrieval latency;
- language model response time;
- container health;
- preprocessing duration;
- resource utilization;
- application errors.

In addition to traditional metrics, centralized logging and distributed tracing would simplify debugging by allowing developers to follow requests across multiple services.

Improved observability would also facilitate capacity planning and early detection of operational issues.

---

## 10.8 Deployment Reliability

Although the current deployment process is fully automated, additional mechanisms could improve operational resilience.

One particularly valuable enhancement would be automated rollback support.

If deployment verification detects a failed deployment, the infrastructure could automatically restore the previously deployed container images without requiring manual intervention.

Possible future deployment improvements include:

- automatic rollback;
- deployment health checks;
- blue-green deployments;
- canary deployments.

These techniques would further reduce deployment risk while improving service availability.

---

## 10.9 Security and Dependency Management

Future versions of the CI/CD pipeline should include automated security validation.

Potential improvements include:

- dependency vulnerability scanning;
- container image scanning;
- Software Bill of Materials (SBOM) generation;
- license compliance verification;
- secret detection.

Integrating these checks into Continuous Integration would allow vulnerabilities to be identified before deployment, improving the overall security posture of the platform.

---

## 10.10 Scalability

The current deployment architecture is well suited to the expected workload of the project.

However, future growth in the number of users or indexed documents may require additional scalability mechanisms.

Potential areas of improvement include:

- horizontal scaling of application services;
- distributed retrieval infrastructure;
- load balancing across multiple application instances;
- caching frequently retrieved information;
- asynchronous preprocessing workflows.

These enhancements would improve the platform's ability to handle larger datasets and higher request volumes while maintaining responsive performance.

---

## 10.11 Research Opportunities

Beyond engineering improvements, the CISUC Chatbot also provides a foundation for continued research into Retrieval-Augmented Generation and conversational AI.

Potential research directions include:

- comparison of alternative retrieval algorithms;
- evaluation of different embedding models;
- adaptive retrieval strategies;
- prompt optimization techniques;
- multilingual retrieval and generation;
- domain-specific language model fine-tuning;
- automatic query expansion;
- personalized retrieval based on user context.

Because the platform separates preprocessing, retrieval, and language generation into independent components, it provides a flexible experimental environment for evaluating new techniques without requiring major architectural changes.

---

## 10.12 Concluding Remarks

The CISUC Chatbot demonstrates how modern Retrieval-Augmented Generation techniques can be combined with automated preprocessing, hybrid retrieval, containerized deployment, and continuous delivery to provide conversational access to institutional knowledge.

The platform was designed with modularity, reproducibility, and maintainability as guiding principles. By separating preprocessing from runtime execution, adopting a hybrid retrieval strategy, and automating software delivery through a CI/CD pipeline, the project establishes a solid foundation for both operational use and future development.

While several opportunities remain for improvement—including automated evaluation, incremental preprocessing, enhanced observability, and deployment resilience—the existing architecture has been designed to accommodate these extensions with minimal disruption to the overall system.

As both Large Language Models and information retrieval techniques continue to evolve, the modular architecture of the CISUC Chatbot provides a flexible platform capable of incorporating future advances while maintaining a consistent and reproducible development workflow.
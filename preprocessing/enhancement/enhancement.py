"""
Enhancement Module

This module implements the semantic enrichment pipeline for the CISUC RAG system.
It utilizes OpenAI's GPT models to:
- Generate concise summaries for research groups and individual researchers.
- Identify and summarize scientific expertise and research focus.
- Build relational mappings between researchers and their respective groups.
- Produce enriched Markdown archives and relational CSV files for ingestion.
"""

import json
import os
import time
import pandas as pd
from openai import OpenAI
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
import argparse
import shutil

from preprocessing.paths import (
    RAW_DIR,
    ENRICHED_DIR,
    ensure_workspace_directories,
)

OPENAI_MODEL = os.environ["ENHANCEMENT_MODEL"]

MAX_WORKERS = int(
    os.environ["ENHANCEMENT_MAX_WORKERS"]
)

api_key = os.environ["OPENAI_API_KEY"]

if not api_key:
    raise RuntimeError("OPENAI_API_KEY is not set")

# ---------------------------------------------------------
# Initial Configuration
# ---------------------------------------------------------
client = OpenAI(api_key=api_key)

# ---------------------------------------------------------
# OpenAI Interaction Functions
# ---------------------------------------------------------

def generate_short_description(entity_type: str, text_content: str) -> str:
    """
    Generate a 2-3 sentence summary for a given entity based on its content.

    Args:
        entity_type: The type of entity (e.g., 'researcher', 'group').
        text_content: The raw text content to be summarized.

    Returns:
        str: A concise summary or an error message if generation failed.
    """
    if not text_content or len(text_content.strip()) < 10:
        return "Insufficient information to generate description."

    prompt = f"Summarize the following information about a {entity_type} in a maximum of 2 to 3 concise sentences:\n\n{text_content}"

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": "You are an assistant specialized in summarizing academic profiles and research groups for RAG systems. Be direct and factual."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3
            )
            choice = response.choices[0]
            if choice.message.content:
                return choice.message.content.strip()
            return "Empty response from API."
        except Exception as e:
            if "429" in str(e) or "rate limit" in str(e).lower():
                espera = 2 ** attempt
                print(f"Rate limit atingido. A esperar {espera} segundos antes de tentar novamente...")
                time.sleep(espera)
            else:
                print(f"Erro na API da OpenAI: {e}")
                return "Error generating description."

    return "Error generating description (Max retries exceeded)."

def generate_research_focus(titulos_publicacoes: list[str]) -> str:
    """
    Synthesize a researcher's scientific expertise into a single sentence based on their publications.

    Args:
        titulos_publicacoes: A list of publication titles.

    Returns:
        str: A single concise summary sentence.
    """
    if not titulos_publicacoes:
        return "No publications available to determine research focus."

    titulos_texto = "\n- ".join(titulos_publicacoes[:20])

    prompt = f"Based on the following publication titles authored by a researcher, write a single concise sentence summarizing their main areas of scientific expertise and research focus:\n\n- {titulos_texto}"

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": "You are a scientific assistant analyzing academic publications."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3
            )
            choice = response.choices[0]
            if choice.message.content:
                return choice.message.content.strip()
            return "Empty response from API."
        except Exception as e:
            if "429" in str(e) or "rate limit" in str(e).lower():
                espera = 2 ** attempt
                time.sleep(espera)
            else:
                return "Error analyzing research focus."

    return "Error analyzing research focus (Max retries exceeded)."

# ---------------------------------------------------------
# Parsing and Processing Functions
# ---------------------------------------------------------


def generate_name_aliases(full_name: str) -> list[str]:
    """
    Generate common name permutations for academic indexing and matching.

    Example: João Rodrigues Campos -> ['João Campos', 'J. Campos', 'J. R. Campos']

    Args:
        full_name: The researcher's full name.

    Returns:
        list[str]: A list of generated aliases.
    """
    if not full_name:
        return []

    parts = full_name.strip().split()
    if len(parts) <= 1:
        return []

    aliases: set[str] = set()
    primeiro = parts[0]
    ultimo = parts[-1]
    meios = parts[1:-1]

    # 1. First + Last
    aliases.add(f"{primeiro} {ultimo}")

    # 2. First Initial + Last
    aliases.add(f"{primeiro[0]}. {ultimo}")

    # 3. First Initial + Middle Initials + Last
    if meios:
        iniciais_meio = " ".join([m[0] + "." for m in meios])
        aliases.add(f"{primeiro[0]}. {iniciais_meio} {ultimo}")

    # Remove the exact full name if it was accidentally generated
    if full_name in aliases:
        aliases.remove(full_name)

    return list(aliases)

def process_groups(group_files_paths: list[str]) -> dict[str, dict[str, str]]:
    """
    Summarize all research groups by processing their Markdown files.

    Args:
        group_files_paths: List of absolute or relative paths to group MD files.

    Returns:
        dict[str, dict[str, str]]: A mapping of group slugs to their content and AI summary.
    """
    groups_data: dict[str, dict[str, str]] = {}

    for filepath in group_files_paths:
        if not os.path.exists(filepath):
            continue

        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()

        group_slug = os.path.basename(filepath).replace('.md', '')
        print(f"A gerar resumo para o grupo: {group_slug}...")
        description = generate_short_description("Artificial Intelligence research group", content)

        groups_data[group_slug] = {
            'content': content,
            'description': description
        }

    return groups_data

def get_user_items_flat(items_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Deduplicate and flatten nested item structures (publications/projects) from API data.

    Args:
        items_list: Nested list of items as returned by the API retriever.

    Returns:
        list[dict[str, Any]]: A flat list of unique items.
    """
    flat_items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    if not items_list:
        return flat_items

    for group in items_list:
        for item in group.get('data', []):
            item_id = item.get('id')

            if item_id and item_id not in seen_ids:
                seen_ids.add(item_id)
                flat_items.append(item)

            elif not item_id:
                titulo = item.get('title', item.get('headline', ''))
                if titulo and titulo not in seen_ids:
                    seen_ids.add(titulo)
                    flat_items.append(item)

    return flat_items


def extract_all_unique_projects(projs_json_path: str | Path) -> dict[str, dict[str, Any]]:
    """
    Extract a comprehensive mapping of unique projects across all users.

    Args:
        projs_json_path: Path to the projects JSON file.

    Returns:
        dict[str, dict[str, Any]]: Mapping of project IDs to project data.
    """
    with open(projs_json_path, 'r', encoding='utf-8') as f:
        projs_raw = json.load(f)

    unique_projects: dict[str, dict[str, Any]] = {}
    for user_data in projs_raw.get('data', []):
        for group in user_data.get('projects', []):
            for proj in group.get('data', []):
                p_id = proj.get('id')
                if p_id and p_id not in unique_projects:
                    unique_projects[p_id] = proj

    print(f"Extraídos {len(unique_projects)} projetos únicos.")
    return unique_projects

def process_single_user(user: dict[str, Any],
                        dict_publicacoes: dict[str, list[dict[str, Any]]],
                        dict_projetos: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """
    Enrich an individual user profile with AI summaries and flattened related data.

    Args:
        user: The raw user dictionary from the API.
        dict_publicacoes: Mapping of user IDs to nested publication lists.
        dict_projetos: Mapping of user IDs to nested project lists.

    Returns:
        dict[str, Any]: The enriched user dictionary.
    """
    nome = user.get('name', 'Unknown')
    user_id = user.get('id', '')
    bio = user.get('bio', '')
    interesses = user.get('research_interests', [])

    # Generate and store aliases
    user['aliases'] = generate_name_aliases(nome)

    contexto_user = f"Bio: {bio}\nInterests: {', '.join(interesses) if interesses else 'None'}"
    user_desc = generate_short_description("university researcher/student", contexto_user)
    user['short_description'] = user_desc

    pubs_agrupadas = dict_publicacoes.get(user_id, [])
    flat_pubs = get_user_items_flat(pubs_agrupadas)
    user['publications_list'] = flat_pubs

    projs_agrupados = dict_projetos.get(user_id, [])
    flat_projs = get_user_items_flat(projs_agrupados)
    user['projects_list'] = flat_projs

    titulos = [p.get('headline', '') for p in flat_pubs if p.get('headline')]
    user['ai_research_focus'] = generate_research_focus(titulos)

    return user

def process_users_and_create_relations(
    users_json_path: str | Path,
    pubs_json_path: str | Path,
    projs_json_path: str | Path,
    groups_data: dict[str, dict[str, str]],
    limit: int | None = None,
) -> tuple[list[dict[str, Any]], pd.DataFrame]:
    """
    Batch process all users in parallel and construct a relational DataFrame linking researchers to groups.

    Args:
        users_json_path: Source JSON for users.
        pubs_json_path: Source JSON for publications.
        projs_json_path: Source JSON for projects.
        groups_data: Previously processed group summaries.
        limit: Maximum number of users to process.

    Returns:
        tuple[list[dict], pd.DataFrame]: A list of enriched users and the relational mapping DataFrame.
    """
    with open(users_json_path, 'r', encoding='utf-8') as f:
        users_raw = json.load(f)

    users_list = users_raw.get('data', [])
    if limit is not None:
        users_list = users_list[:limit]

    with open(pubs_json_path, 'r', encoding='utf-8') as f:
        pubs_raw = json.load(f)
    dict_publicacoes = { pub_user.get('id'): pub_user.get('publications', []) for pub_user in pubs_raw.get('data', []) }

    with open(projs_json_path, 'r', encoding='utf-8') as f:
        projs_raw = json.load(f)
    dict_projetos = { proj_user.get('id'): proj_user.get('projects', []) for proj_user in projs_raw.get('data', []) }

    enriched_users: list[dict[str, Any]] = []

    print(f"\nA iniciar processamento paralelo de {len(users_list)} utilizadores.\n")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(process_single_user, user, dict_publicacoes, dict_projetos): user for user in users_list}

        completados = 0
        total = len(users_list)
        for future in as_completed(futures):
            completados += 1
            enriched_users.append(future.result())

            if completados % 100 == 0 or completados == total:
                print(f"Progresso: {completados}/{total} utilizadores processados...")

    print("\nProcessamento de resumos, publicações e projetos concluído! A gerar tabela relacional...")

    relational_rows: list[dict[str, str]] = []
    for user in enriched_users:
        nome = user.get('name', 'Unknown')
        user_desc = user.get('short_description', '')
        grupos_do_user = user.get('research_groups', [])

        if not grupos_do_user:
            relational_rows.append({
                'User Name': nome,
                'User Description': user_desc,
                'Group Acronym': 'None',
                'Group Name': 'None',
                'Group Description': 'No associated group.'
            })

        for grupo in grupos_do_user:
            grupo_nome = grupo.get('name', '')
            grupo_acronym = grupo.get('acronym', '').lower()

            grupo_info = groups_data.get(grupo_acronym, {})
            grupo_desc = grupo_info.get('description', 'Description not available (MD not found).')

            relational_rows.append({
                'User Name': nome,
                'User Description': user_desc,
                'Group Acronym': grupo_acronym,
                'Group Name': grupo_nome,
                'Group Description': grupo_desc
            })

    return enriched_users, pd.DataFrame(relational_rows)

# ---------------------------------------------------------
# Final Output Generation (Enriched)
# ---------------------------------------------------------
def generate_enriched_markdowns(
    enriched_users: list[dict[str, Any]],
    groups_data: dict[str, dict[str, str]],
    df_relations: pd.DataFrame,
    output_dir: Path,
) -> None:
    """
    Generate the final enriched Markdown files for users and research groups.

    Args:
        enriched_users: List of user dictionaries containing AI summaries.
        groups_data: Mapping of group data and AI summaries.
        df_relations: DataFrame containing relational group/user information.
        output_dir: Directory where the generated Markdown files are written.
    """
    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    users_md_path = output_dir / "enriched_api-users.md"
    with open(users_md_path, 'w', encoding='utf-8') as f:
        f.write("# Researchers and Users (Enriched)\n\n")

        for user in enriched_users:
            f.write(f"## {user.get('name', 'Unknown')}\n")

            # Inject aliases into the MD
            aliases = user.get('aliases', [])
            if aliases:
                f.write(f"**Aliases / Known As:** {', '.join(aliases)}\n")

            f.write(f"**Id:** {user.get('id', 'N/A')}\n")
            f.write(f"**Email:** {user.get('email', 'N/A')}\n")
            f.write(f"**ORCID:** {user.get('orcid', 'N/A')}\n")
            f.write(f"**CV:** {user.get('cv', 'N/A')}\n")
            f.write(f"**Short Description:** {user.get('short_description', 'N/A')}\n")
            f.write(f"**Research Focus:** {user.get('ai_research_focus', 'N/A')}\n")

            if user.get('laboratories'):
                for lab in user.get('laboratories', []):
                    f.write(f"**Laboratory:** {lab.get('name', 'N/A')}\n")

            if user.get('research_groups'):
                f.write("**Research Groups:**\n")
                for grupo in user.get('research_groups', []):
                    acronym = grupo.get('acronym', '').lower()
                    g_desc = groups_data.get(acronym, {}).get('description', 'No description.')
                    f.write(f"  - **{grupo.get('name')} ({acronym.upper()}):** {g_desc}\n")

            publicacoes = user.get('publications_list', [])
            f.write("\n### Publications\n")
            if publicacoes:
                for pub in publicacoes:
                    titulo = pub.get('headline', 'Unknown Publication').strip()
                    ano = pub.get('year', 'Unknown Year')
                    f.write(f"- **[{ano}]** {titulo}\n")
            else:
                f.write("- No public records found.\n")

            projetos = user.get('projects_list', [])
            f.write("\n### Projects\n")
            if projetos:
                for proj in projetos:
                    titulo = proj.get('title', 'Unknown Title')
                    keywords = proj.get('keywords', 'N/A')
                    financiador = proj.get('founded_by', 'N/A')
                    data_inicio = proj.get('start_date', '')
                    data_fim = proj.get('end_date', 'Present')

                    f.write(f"#### {titulo}\n")
                    f.write(f"- **Period:** {data_inicio} to {data_fim}\n")
                    f.write(f"- **Keywords:** {keywords}\n")
                    f.write(f"- **Funded By:** {financiador}\n")

                    desc = proj.get('description', '')
                    if desc:
                        # Extra cleaning for common HTML tags
                        desc_limpa = desc.replace('<p>', '').replace('</p>', '').replace('<br>', '\n').replace('<br />', '\n').strip()
                        f.write(f"- **Description:** {desc_limpa}\n")
                    f.write("\n")
            else:
                f.write("- No projects found.\n")

            f.write("\n" + "-"*80 + "\n\n")

    print(f"Ficheiro de utilizadores guardado em: {users_md_path}")

    for slug, data in groups_data.items():
        group_md_path = output_dir / f"enriched_{slug}.md"
        with open(group_md_path, 'w', encoding='utf-8') as f:
            f.write(f"# Group: {slug.upper()}\n\n")
            f.write(f"**AI Short Description:** {data['description']}\n\n")

            f.write("## Associated Researchers\n")
            membros = df_relations[df_relations['Group Acronym'] == slug]
            for _, row in membros.iterrows():
                f.write(f"- **{row['User Name']}:** {row['User Description']}\n")

            f.write("\n## Original Content\n")
            f.write(data['content'])

        print(f"Ficheiro do grupo guardado em: {group_md_path}")

def generate_projects_markdown(
    unique_projects: dict[str, dict[str, Any]],
    user_desc_map: dict[str, str],
    output_dir: Path,
) -> None:
    """
    Generate a unified Markdown file containing all unique research projects.

    Args:
        unique_projects: Mapping of project IDs to project metadata.
        user_desc_map: Mapping of user IDs to their AI-generated descriptions.
        output_dir: Directory where the generated Markdown files are written.
    """
    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )
    projs_md_path = output_dir / "enriched_projects_full.md"

    with open(projs_md_path, 'w', encoding='utf-8') as f:
        f.write("# CISUC Research Projects\n\n")

        for p_id, proj in unique_projects.items():
            titulo = proj.get('title', 'Unknown Title')
            f.write(f"## {titulo}\n")
            f.write(f"**Project ID:** {p_id}\n")
            f.write(f"**Period:** {proj.get('start_date', '')} to {proj.get('end_date', 'Present')}\n")
            f.write(f"**Keywords:** {proj.get('keywords', 'N/A')}\n")
            f.write(f"**Funded By:** {proj.get('founded_by', 'N/A')}\n")

            desc = proj.get('description', '')
            if desc:
                desc_limpa = desc.replace('<p>', '').replace('</p>', '').replace('<br>', '\n').replace('<br />', '\n').strip()
                f.write(f"**Description:** {desc_limpa}\n\n")

            membros = proj.get('members', [])
            if membros:
                f.write("### Associated Members\n")
                for membro in membros:
                    m_id = membro.get('id', '')
                    m_nome = membro.get('name', 'Unknown')
                    m_bio = user_desc_map.get(m_id, "Researcher at CISUC.")
                    f.write(f"- **{m_nome}:** {m_bio}\n")

            f.write("\n" + "-"*80 + "\n\n")

    print(f"Ficheiro completo de projetos guardado em: {projs_md_path}")

def get_group_files_from_json(json_path: str | Path, base_folder_path: str | Path) -> list[str]:
    """
    Identify and resolve filesystem paths for all research groups mentioned in the user JSON.

    Args:
        json_path: Path to the users JSON.
        base_folder_path: Base directory where group MD files are stored.

    Returns:
        list[str]: A list of resolved absolute/relative paths to the found MD files.
    """
    with open(json_path, 'r', encoding='utf-8') as f:
        users_raw = json.load(f)

    acronimos: set[str] = set()
    for user in users_raw.get('data', []):
        for grupo in user.get('research_groups', []):
            acronimo = grupo.get('acronym')
            if acronimo:
                acronimos.add(acronimo.lower())

    print(f"Encontrados {len(acronimos)} grupos distintos no JSON.")

    group_files_paths: list[str] = []
    pasta_principal = Path(base_folder_path)

    for acronimo in acronimos:
        ficheiros_encontrados = list(pasta_principal.rglob(f"{acronimo}.md"))
        if ficheiros_encontrados:
            caminho_ficheiro = str(ficheiros_encontrados[0])
            group_files_paths.append(caminho_ficheiro)
        else:
            print(f"Aviso: Não encontrei nenhum ficheiro para o grupo '{acronimo}'.")

    return group_files_paths

if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--limit",
        type=int,
        help="Only process the first N groups/users/projects.",
    )

    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be greater than zero")

    output_dir = ENRICHED_DIR

    if args.limit is not None:
        output_dir = ENRICHED_DIR / "dry-run"

        if output_dir.exists():
            shutil.rmtree(output_dir)

        print(f"Running smoke test with limit={args.limit}")
        print(f"Writing output to {output_dir}")

    print("A iniciar pipeline de enriquecimento CISUC RAG...")
    ensure_workspace_directories()

    # Configuration paths
    USERS_JSON = RAW_DIR / "api" / "api-users.json"
    PUBS_JSON = RAW_DIR / "api" / "api-publications.json"
    PROJS_JSON = RAW_DIR / "api" / "api-projects.json"
    PASTA_GRUPOS = RAW_DIR / "static" / "research"

    required_inputs = [
        USERS_JSON,
        PUBS_JSON,
        PROJS_JSON,
        PASTA_GRUPOS,
    ]

    missing_inputs = [
        path
        for path in required_inputs
        if not path.exists()
    ]

    if missing_inputs:
        missing_text = "\n".join(
            f"  - {path}"
            for path in missing_inputs
        )
        raise FileNotFoundError(
            "Missing enhancement inputs:\n"
            f"{missing_text}"
        )

    # 1. Resolve group files
    group_files = get_group_files_from_json(USERS_JSON, PASTA_GRUPOS)
    if args.limit is not None:
        group_files = group_files[:args.limit]
    print(f"Ficheiros encontrados: {group_files}")

    # 2. Process groups and generate summaries
    groups_info = process_groups(group_files)

    # 3. Extract unique projects
    all_projects = extract_all_unique_projects(PROJS_JSON)
    if args.limit is not None:
        all_projects = dict(
            list(all_projects.items())[:args.limit]
        )

    # 4. Process users and create relational mapping
    list_enriched_users, relations_df = process_users_and_create_relations(
        USERS_JSON,
        PUBS_JSON,
        PROJS_JSON,
        groups_info,
        limit=args.limit,
    )

    # 5. Build user description map for project linking
    user_descriptions = {user.get('id', ''): user.get('short_description', 'Associated Researcher.') for user in list_enriched_users}

    # 6. Save relational CSV
    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )
    relations_path = output_dir / "relacoes_grupo_investigador.csv"

    relations_df.to_csv(
        relations_path,
        index=False,
        encoding="utf-8",
    )
    print(
        "\nTabela relacional guardada em: "
        f"{relations_path}"
    )
    # 7. Generate final Markdown archives
    generate_enriched_markdowns(
        list_enriched_users,
        groups_info,
        relations_df,
        output_dir,
    )

    generate_projects_markdown(
        all_projects,
        user_descriptions,
        output_dir,
    )

    print("\nPipeline concluído com sucesso!")
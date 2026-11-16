"""Синхронизация Issues и GitHub Project; авторизация через GH_TOKEN для gh."""

import argparse
import json
import os
import subprocess
from datetime import datetime


ISSUE_FIELDS = """
id number state closedAt repository { nameWithOwner }
timelineItems(last: 1, itemTypes: [CLOSED_EVENT, REOPENED_EVENT]) {
  nodes { ... on ClosedEvent { createdAt } ... on ReopenedEvent { createdAt } }
}
"""
ITEM_FIELDS = """
id isArchived
fieldValueByName(name: "Status") {
  ... on ProjectV2ItemFieldSingleSelectValue { name createdAt updatedAt }
}
content {
  __typename
  ... on Issue { %s }
}
""" % ISSUE_FIELDS


def graphql(query, **variables):
    """Передаём JSON через stdin, чтобы значения не попадали в командную строку."""
    result = subprocess.run(
        ["gh", "api", "graphql", "--input", "-"],
        input=json.dumps({"query": query, "variables": variables}),
        text=True, encoding="utf-8", capture_output=True, check=True,
    )
    response = json.loads(result.stdout)
    if response.get("errors"):
        raise RuntimeError(json.dumps(response["errors"], ensure_ascii=False))
    return response["data"]


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def desired_change(issue, status):
    """Последнее изменение статуса побеждает; при равенстве — состояние Issue."""
    closed = issue["state"] == "CLOSED"
    if not status:
        return ("status", "Done" if closed else "Todo")
    name = status["name"]
    if name not in {"Todo", "In Progress", "Done"}:
        raise ValueError(f"Неизвестный статус: {name}")
    if closed == (name == "Done"):
        return None
    events = issue["timelineItems"]["nodes"]
    issue_changed = events[-1]["createdAt"] if events else issue.get("closedAt")
    # Начальное Todo от встроенного Item added не переоткрывает старую задачу.
    initial_todo = name == "Todo" and status["createdAt"] == status["updatedAt"]
    project_newer = not initial_todo and (
        not issue_changed or timestamp(status["updatedAt"]) > timestamp(issue_changed)
    )
    if project_newer:
        return ("issue", "CLOSED" if name == "Done" else "OPEN")
    return ("status", "Done" if closed else "Todo")


class Sync:
    def __init__(self, owner, repo_name, project_number, dry_run=False):
        self.owner = owner
        self.repo_name = repo_name
        self.repository = f"{owner}/{repo_name}"
        self.project_number = project_number
        self.dry_run = dry_run

    def load(self):
        data = graphql("""
        query($owner: String!, $repo: String!, $number: Int!) {
          repository(owner: $owner, name: $repo) { id }
          organization(login: $owner) {
            projectV2(number: $number) {
              id fields(first: 100) {
                pageInfo { hasNextPage }
                nodes { ... on ProjectV2SingleSelectField { id name options { id name } } }
              }
            }
          }
        }
        """, owner=self.owner, repo=self.repo_name, number=self.project_number)
        self.repository_id = data["repository"]["id"]
        project = data["organization"]["projectV2"]
        self.project_id = project["id"]
        if project["fields"]["pageInfo"]["hasNextPage"]:
            raise RuntimeError("Больше 100 полей: требуется пагинация полей")
        field = next(f for f in project["fields"]["nodes"] if f.get("name") == "Status")
        self.field_id = field["id"]
        self.options = {o["name"]: o["id"] for o in field["options"]}
        if not {"Todo", "In Progress", "Done"}.issubset(self.options):
            raise RuntimeError("В Project нужны статусы Todo, In Progress и Done")

    def items(self):
        cursor = None
        result = []
        while True:
            data = graphql("""
            query($id: ID!, $cursor: String) {
              node(id: $id) { ... on ProjectV2 {
                items(first: 100, after: $cursor) {
                  pageInfo { hasNextPage endCursor } nodes { %s }
                }
              } }
            }
            """ % ITEM_FIELDS, id=self.project_id, cursor=cursor)
            page = data["node"]["items"]
            result.extend(page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:
                return result
            cursor = page["pageInfo"]["endCursor"]

    def issues(self):
        cursor = None
        result = []
        while True:
            data = graphql("""
            query($id: ID!, $cursor: String) {
              node(id: $id) { ... on Repository {
                issues(first: 100, after: $cursor) {
                  pageInfo { hasNextPage endCursor } nodes { %s }
                }
              } }
            }
            """ % ISSUE_FIELDS, id=self.repository_id, cursor=cursor)
            page = data["node"]["issues"]
            result.extend(page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:
                return result
            cursor = page["pageInfo"]["endCursor"]

    def mutation(self, description, query, **variables):
        print(description, flush=True)
        if not self.dry_run:
            return graphql(query, **variables)
        return None

    def set_status(self, item_id, name):
        self.mutation(f"Карточка {item_id}: статус → {name}", """
        mutation($project: ID!, $item: ID!, $field: ID!, $option: String!) {
          updateProjectV2ItemFieldValue(input: {
            projectId: $project, itemId: $item, fieldId: $field,
            value: {singleSelectOptionId: $option}
          }) { projectV2Item { id } }
        }
        """, project=self.project_id, item=item_id, field=self.field_id,
                      option=self.options[name])

    def run(self):
        self.load()
        items = self.items()
        issues = self.issues()
        members = {i["content"]["id"] for i in items
                   if (i.get("content") or {}).get("__typename") == "Issue"}
        for issue in issues:
            if issue["id"] not in members:
                data = self.mutation(f"Добавление Issue #{issue['number']} в Project", """
                mutation($project: ID!, $issue: ID!) {
                  addProjectV2ItemById(input: {projectId: $project, contentId: $issue}) {
                    item { id }
                  }
                }
                """, project=self.project_id, issue=issue["id"])
                if data:
                    self.set_status(data["addProjectV2ItemById"]["item"]["id"],
                                    "Done" if issue["state"] == "CLOSED" else "Todo")

        for item in items:
            content = item.get("content") or {}
            if item["isArchived"]:
                continue
            if content.get("__typename") == "DraftIssue":
                data = self.mutation(f"Преобразование черновика {item['id']} в Issue", """
                mutation($item: ID!, $repo: ID!) {
                  convertProjectV2DraftIssueItemToIssue(input: {
                    itemId: $item, repositoryId: $repo
                  }) { item { id } }
                }
                """, item=item["id"], repo=self.repository_id)
                if not data:
                    continue
            elif content.get("__typename") != "Issue":
                continue
            # Перед записью обновляем снимок, включая последние события Issue.
            fresh = graphql("""
            query($id: ID!) { node(id: $id) { ... on ProjectV2Item { %s } } }
            """ % ITEM_FIELDS, id=item["id"])["node"]
            content = fresh.get("content") or {}
            if (fresh["isArchived"] or content.get("__typename") != "Issue"
                    or content["repository"]["nameWithOwner"] != self.repository):
                continue
            change = desired_change(content, fresh["fieldValueByName"])
            if not change:
                continue
            kind, value = change
            if kind == "status":
                self.set_status(item["id"], value)
            else:
                mutation_name = "closeIssue" if value == "CLOSED" else "reopenIssue"
                self.mutation(f"Issue #{content['number']}: состояние → {value}", """
                mutation($id: ID!) {
                  %s(input: {issueId: $id}) { issue { id state } }
                }
                """ % mutation_name, id=content["id"])
        print(f"Проверено: {len(issues)} Issues, {len(items)} карточек; dry_run={self.dry_run}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    owner, repo = os.environ.get("SYNC_REPOSITORY", "Ekanam-Friends/MGIMO-LMS").split("/")
    Sync(owner, repo, int(os.environ.get("SYNC_PROJECT_NUMBER", "4")), args.dry_run).run()


if __name__ == "__main__":
    main()

from __future__ import annotations

from dataclasses import dataclass

from .world_model import PersonalWorldModel, WorldFact


@dataclass(frozen=True)
class WorldRelation:
    subject: str
    predicate: str
    object_id: str
    confidence: float
    source: str


@dataclass(frozen=True)
class WorldNeighborhood:
    root: str
    facts: tuple[WorldFact, ...]
    relations: tuple[WorldRelation, ...]
    entities: tuple[str, ...]


class PersonalWorldGraph:
    """Graph semantics layered over the authoritative PersonalWorldModel store."""

    _relation_prefix = "relation:"

    def __init__(self, world: PersonalWorldModel) -> None:
        self.world = world

    def connect(
        self,
        subject: str,
        predicate: str,
        object_id: str,
        *,
        confidence: float = 1.0,
        source: str = "runtime",
    ) -> WorldRelation:
        subject = subject.strip()
        predicate = predicate.strip().casefold().replace(" ", "_")
        object_id = object_id.strip()
        if not subject or not predicate or not object_id:
            raise ValueError("subject, predicate and object_id are required")
        attribute = f"{self._relation_prefix}{predicate}:{object_id}"
        self.world.observe(subject, attribute, True, confidence=confidence, source=source)
        return WorldRelation(subject, predicate, object_id, confidence, source)

    def set_property(
        self,
        entity: str,
        attribute: str,
        value: object,
        *,
        confidence: float = 1.0,
        source: str = "runtime",
    ) -> None:
        if attribute.startswith(self._relation_prefix):
            raise ValueError("relation attributes must be created with connect()")
        self.world.observe(entity, attribute, value, confidence=confidence, source=source)

    @classmethod
    def _relation(cls, fact: WorldFact) -> WorldRelation | None:
        if not fact.attribute.startswith(cls._relation_prefix) or fact.value is not True:
            return None
        remainder = fact.attribute[len(cls._relation_prefix) :]
        predicate, separator, object_id = remainder.partition(":")
        if not separator or not predicate or not object_id:
            return None
        return WorldRelation(fact.entity, predicate, object_id, fact.confidence, fact.source)

    def relations(self, subject: str | None = None, *, limit: int = 500) -> tuple[WorldRelation, ...]:
        facts = self.world.snapshot(prefix=subject, limit=limit) if subject else self.world.snapshot(limit=limit)
        values = [relation for fact in facts if (relation := self._relation(fact)) is not None]
        if subject:
            values = [item for item in values if item.subject == subject]
        return tuple(values)

    def neighbors(self, entity: str, *, depth: int = 2, max_entities: int = 64) -> WorldNeighborhood:
        if not 0 <= depth <= 5:
            raise ValueError("depth must be between 0 and 5")
        if not 1 <= max_entities <= 256:
            raise ValueError("max_entities must be between 1 and 256")
        all_facts = self.world.snapshot(limit=500)
        all_relations = tuple(
            relation for fact in all_facts if (relation := self._relation(fact)) is not None
        )
        visited = {entity}
        frontier = {entity}
        selected_relations: list[WorldRelation] = []
        for _ in range(depth):
            next_frontier: set[str] = set()
            for relation in all_relations:
                if relation.subject in frontier:
                    selected_relations.append(relation)
                    if len(visited) < max_entities:
                        next_frontier.add(relation.object_id)
                elif relation.object_id in frontier:
                    selected_relations.append(relation)
                    if len(visited) < max_entities:
                        next_frontier.add(relation.subject)
            next_frontier -= visited
            visited.update(next_frontier)
            frontier = next_frontier
            if not frontier:
                break
        facts = tuple(
            fact
            for fact in all_facts
            if fact.entity in visited and not fact.attribute.startswith(self._relation_prefix)
        )
        unique_relations = tuple(dict.fromkeys(selected_relations))
        return WorldNeighborhood(entity, facts, unique_relations, tuple(sorted(visited)))

    def grounding_lines(self, entity: str, *, depth: int = 2) -> tuple[str, ...]:
        neighborhood = self.neighbors(entity, depth=depth)
        lines = [
            f"{fact.entity}.{fact.attribute}={fact.value!r} "
            f"(confidence={fact.confidence:.2f}, source={fact.source})"
            for fact in neighborhood.facts
        ]
        lines.extend(
            f"{item.subject} --{item.predicate}--> {item.object_id} "
            f"(confidence={item.confidence:.2f}, source={item.source})"
            for item in neighborhood.relations
        )
        return tuple(lines)

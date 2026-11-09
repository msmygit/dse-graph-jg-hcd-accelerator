// Template: food
// Covers: Uuid partition key, MV index, searchIndex (text + string),
//         setOf, mapOf, geo Point, edge properties.
// Tokens: {{GRAPH_NAME}}, {{VERTEX_COUNT}}, {{EDGE_COUNT}}

system.graph('{{GRAPH_NAME}}').ifNotExists()
  .withReplication("{'class':'SimpleStrategy','replication_factor':1}")
  .andDurableWrites(true).create()

schema = {{GRAPH_NAME}}.schema()

schema.vertexLabel('person').ifNotExists()
  .partitionBy('person_id', Uuid)
  .property('name', Text)
  .property('age', Int)
  .property('nickname', setOf(Text))
  .property('badge', mapOf(Text, Date))
  .create()

schema.vertexLabel('recipe').ifNotExists()
  .partitionBy('recipe_id', Uuid)
  .property('name', Text)
  .property('instructions', Text)
  .property('cuisine', Text)
  .property('tags', setOf(Text))
  .create()

schema.vertexLabel('location').ifNotExists()
  .partitionBy('location_id', Uuid)
  .property('name', Text)
  .property('geo_point', Point)
  .create()

schema.edgeLabel('created').ifNotExists()
  .from('person').to('recipe').property('created_on', Date).create()

schema.edgeLabel('knows').ifNotExists()
  .from('person').to('person').property('since', Date).create()

schema.edgeLabel('located_at').ifNotExists()
  .from('person').to('location').create()

schema.vertexLabel('person').materializedView('person_by_name').ifNotExists()
  .partitionBy('name').clusterBy('person_id', Asc).create()

schema.vertexLabel('recipe').searchIndex().ifNotExists()
  .by('instructions').asText().by('cuisine').asString().create()

schema.vertexLabel('location').searchIndex().ifNotExists()
  .by('geo_point').create()

schema.edgeLabel('knows').from('person').to('person')
  .materializedView('knows_by_since').ifNotExists()
  .partitionBy(OUT, 'person_id').clusterBy('since', Asc)
  .clusterBy(IN, 'person_id', Asc).create()

g = {{GRAPH_NAME}}.traversal()

{{VERTEX_COUNT}}.times {
  g.addV('person')
    .property('person_id', UUID.randomUUID())
    .property('name', "Person_${it}")
    .property('age', (int)(Math.random() * 60 + 20))
    .property('nickname', (it % 3 == 0 ? ["p${it}", 'chef'] : ["p${it}"]) as Set)
    .property('badge', it % 2 == 0 ? ['gold': java.time.LocalDate.now(), 'early': java.time.LocalDate.of(2020, 1, 1)] : [:])
    .iterate()
}

recipeCount = Math.min({{VERTEX_COUNT}}, 500)
recipeCount.times {
  g.addV('recipe')
    .property('recipe_id', UUID.randomUUID())
    .property('name', "Recipe_${it}")
    .property('instructions', "Step 1: Prepare ingredients for recipe ${it}. Step 2: Cook.")
    .property('cuisine', ['Italian','French','Japanese','Mexican','Indian'].get((int)(Math.random()*5)))
    .property('tags', (it % 2 == 0 ? ['quick', 'veg'] : ['slow']) as Set)
    .iterate()
}

locationCount = Math.max(1, {{VERTEX_COUNT}}.intdiv(10))
locationCount.times {
  g.addV('location')
    .property('location_id', UUID.randomUUID())
    .property('name', "Location_${it}")
    .property('geo_point', Geo.point(-120.0 + it, 30.0 + it * 0.5))
    .iterate()
}

// {{EDGE_COUNT}} distinct pairs per edge label. DSG keys an edge row by its (out, in) pair, so pairs must be unique:
// knows: person k -> person k + 1 + k.intdiv(n) (mod n); created: person k -> recipe (k + k.intdiv(n)) mod m.
people  = g.V().hasLabel('person').toList()
recipes = g.V().hasLabel('recipe').toList()
n = people.size()
m = recipes.size()
{{EDGE_COUNT}}.times { k ->
  if (n > 1) {
    g.addE('knows').from(people.get(k % n)).to(people.get((k % n + 1 + k.intdiv(n) % (n - 1)) % n))
      .property('since', java.time.LocalDate.now().minusDays(k)).iterate()
  }
  g.addE('created').from(people.get(k % n)).to(recipes.get((k % n + k.intdiv(n)) % m))
    .property('created_on', java.time.LocalDate.now()).iterate()
}

// Each person is located at one location (re-reads vertices: every blank-line block is a separate request).
places = g.V().hasLabel('location').toList()
g.V().hasLabel('person').toList().eachWithIndex { p, i ->
  g.addE('located_at').from(p).to(places.get(i % places.size())).iterate()
}

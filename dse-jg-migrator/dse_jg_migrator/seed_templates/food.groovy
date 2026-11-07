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
    .iterate()
}

recipeCount = Math.min({{VERTEX_COUNT}}, 500)
recipeCount.times {
  g.addV('recipe')
    .property('recipe_id', UUID.randomUUID())
    .property('name', "Recipe_${it}")
    .property('instructions', "Step 1: Prepare ingredients for recipe ${it}. Step 2: Cook.")
    .property('cuisine', ['Italian','French','Japanese','Mexican','Indian'].get((int)(Math.random()*5)))
    .iterate()
}

people  = g.V().hasLabel('person').limit({{EDGE_COUNT}}).toList()
recipes = g.V().hasLabel('recipe').toList()
people.eachWithIndex { p, i ->
  r = recipes.get((int)(Math.random() * recipes.size()))
  g.addE('created').from(p).to(r).property('created_on', new Date()).iterate()
  if (i + 1 < people.size()) {
    g.addE('knows').from(p).to(people.get(i + 1))
      .property('since', new Date()).iterate()
  }
}

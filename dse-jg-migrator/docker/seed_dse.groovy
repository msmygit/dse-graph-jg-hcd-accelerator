// docker/seed_dse.groovy
// Seeds the 'food' test graph on the DSE source container with all schema patterns
// the assessor and migrator must handle.
//
// Usage:
//   docker exec -i migrator-dse-source dse gremlin-console -e /dev/stdin < docker/seed_dse.groovy

:remote connect tinkerpop.server conf/remote.yaml session
:remote console

// Create the food graph (DSG / Core engine)
system.graph('food').ifNotExists()
  .withReplication("{'class':'SimpleStrategy','replication_factor':1}")
  .andDurableWrites(true)
  .create()

:remote config alias g food.g
schema = food.schema()

// Vertex labels — covering partition keys, clustering, collections, UDTs
schema.vertexLabel('person').ifNotExists()
  .partitionBy('person_id', Uuid)
  .property('name', Text)
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

// Edge labels
schema.edgeLabel('created').ifNotExists()
  .from('person').to('recipe')
  .property('created_on', Date)
  .create()

schema.edgeLabel('knows').ifNotExists()
  .from('person').to('person')
  .property('since', Date)
  .create()

// Indexes — MV, search, vertex-centric
schema.vertexLabel('person').materializedView('person_by_name')
  .ifNotExists().partitionBy('name').clusterBy('person_id', Asc).create()

schema.vertexLabel('recipe').searchIndex().ifNotExists()
  .by('instructions').asText()
  .by('cuisine').asString()
  .create()

schema.edgeLabel('knows').from('person').to('person')
  .materializedView('knows_by_since').ifNotExists()
  .partitionBy(OUT, 'person_id').clusterBy('since', Asc).clusterBy(IN, 'person_id', Asc)
  .create()

// Seed data
p1 = g.addV('person').property('person_id', UUID.randomUUID()).property('name', 'Alice').next()
p2 = g.addV('person').property('person_id', UUID.randomUUID()).property('name', 'Bob').next()
r1 = g.addV('recipe').property('recipe_id', UUID.randomUUID())
        .property('name', 'Pasta').property('instructions', 'Boil water, add pasta')
        .property('cuisine', 'Italian').next()
g.addE('created').from(p1).to(r1).property('created_on', new Date()).iterate()
g.addE('knows').from(p1).to(p2).property('since', new Date()).iterate()

:exit

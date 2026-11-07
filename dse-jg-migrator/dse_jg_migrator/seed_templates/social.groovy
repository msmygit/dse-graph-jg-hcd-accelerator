// Template: social
// Covers: listOf, Timestamp, vertex-centric (RelationIndex), inverse MV edge.
// Tokens: {{GRAPH_NAME}}, {{VERTEX_COUNT}}, {{EDGE_COUNT}}

system.graph('{{GRAPH_NAME}}').ifNotExists()
  .withReplication("{'class':'SimpleStrategy','replication_factor':1}")
  .andDurableWrites(true).create()

schema = {{GRAPH_NAME}}.schema()

schema.vertexLabel('user').ifNotExists()
  .partitionBy('user_id', Uuid)
  .property('username', Text)
  .property('email', Text)
  .property('roles', listOf(Text))
  .property('joined_at', Timestamp)
  .create()

schema.vertexLabel('post').ifNotExists()
  .partitionBy('post_id', Uuid)
  .property('content', Text)
  .property('created_at', Timestamp)
  .create()

schema.edgeLabel('follows').ifNotExists()
  .from('user').to('user').property('followed_at', Timestamp).create()

schema.edgeLabel('authored').ifNotExists()
  .from('user').to('post').property('published_at', Timestamp).create()

schema.vertexLabel('user').materializedView('user_by_username').ifNotExists()
  .partitionBy('username').clusterBy('user_id', Asc).create()

schema.edgeLabel('follows').from('user').to('user')
  .materializedView('follows_inverse').ifNotExists().inverse().create()

schema.edgeLabel('follows').from('user').to('user')
  .materializedView('follows_by_date').ifNotExists()
  .partitionBy(OUT, 'user_id')
  .clusterBy('followed_at', Desc).clusterBy(IN, 'user_id', Asc).create()

g = {{GRAPH_NAME}}.traversal()

{{VERTEX_COUNT}}.times {
  g.addV('user')
    .property('user_id', UUID.randomUUID())
    .property('username', "user_${it}")
    .property('email', "user${it}@example.com")
    .property('joined_at', new Date())
    .iterate()
}

users = g.V().hasLabel('user').limit({{EDGE_COUNT}}).toList()
users.eachWithIndex { u, i ->
  if (i + 1 < users.size()) {
    g.addE('follows').from(u).to(users.get(i + 1))
      .property('followed_at', new Date()).iterate()
  }
}

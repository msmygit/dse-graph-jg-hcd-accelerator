// Template: social
// Covers: listOf, Timestamp, vertex-centric (RelationIndex), inverse MV edge.
// Tokens: {{GRAPH_NAME}}, {{VERTEX_COUNT}}, {{EDGE_COUNT}}; {{V_RANGE}} / {{E_RANGE}} mark a loop the seeder
// submits in chunks (DSE caps each request at 30 s), e.g. (0..<200), (200..<400), … — the index stays global.

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

({{V_RANGE}}).each {
  g.addV('user')
    .property('user_id', UUID.randomUUID())
    .property('username', "user_${it}")
    .property('email', "user${it}@example.com")
    .property('roles', it % 5 == 0 ? ['reader', 'admin'] : ['reader'])
    .property('joined_at', java.time.Instant.now())
    .iterate()
}

users = g.V().hasLabel('user').toList()
n = users.size()
// {{EDGE_COUNT}} distinct follows edges: user k follows user k + 1 + k.intdiv(n) (mod n). DSG keys an edge row by
// its (out, in) pair, so pairs must be unique — guaranteed while {{EDGE_COUNT}} <= n * (n - 1).
if (n > 1) {
  ({{E_RANGE}}).each { k ->
    g.addE('follows').from(users.get(k % n)).to(users.get((k % n + 1 + k.intdiv(n) % (n - 1)) % n))
      .property('followed_at', java.time.Instant.now()).iterate()
  }
}

// One post per user, linked by an authored edge. (Each blank-line-separated block is a separate request,
// so this block re-reads the users instead of reusing `users`.)
users = g.V().hasLabel('user').toList()
({{V_RANGE}}).each { i ->
  u = users.get(i)
  p = g.addV('post')
    .property('post_id', UUID.randomUUID())
    .property('content', "Hello from ${g.V(u).values('username').next()}")
    .property('created_at', java.time.Instant.now())
    .next()
  g.addE('authored').from(u).to(p).property('published_at', java.time.Instant.now()).iterate()
}

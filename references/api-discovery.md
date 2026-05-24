# API discovery strategy

## Preferred sources

Use the richest source available:

1. OpenAPI / Swagger specs
2. framework route declarations
3. handler/controller comments
4. validation schemas and DTO metadata

## Route heuristics

Common route patterns worth scanning:

- Express / Fastify / Koa: `router.get(...)`, `app.post(...)`
- NestJS: `@Controller`, `@Get`, `@Post`, `@Patch`, `@Delete`
- FastAPI: `@app.get`, `@router.post`
- Django / Flask: `path(...)`, `re_path(...)`, `@app.route(...)`
- Laravel: `Route::get`, `Route::prefix`, `Route::middleware`

The generic script should recover method and path even when handler resolution is incomplete.

## Descriptions and params

Prefer real metadata from:

- OpenAPI `summary` and `description`
- OpenAPI path-level and operation-level parameters, including local `$ref`
- OpenAPI request bodies, including local `$ref`, `allOf`, `oneOf`, `anyOf`, nested objects, and arrays of objects
- validator schemas with field descriptions or comments
- DTO/model classes referenced by route handlers, validators, or framework decorators
- comments immediately above route definitions
- auth or middleware markers that imply required headers

If the script only has partial data, keep the output useful rather than empty. A short inferred description is better than no description, but it should be marked as inferred.

## Schema and DTO recovery

When route discovery is used, build a lightweight schema index from the repository instead of only reading the changed route file. This helps incremental sync recover request fields from unchanged DTOs, Zod/Joi schemas, class-validator DTOs, and Pydantic-style models that are referenced by changed route/controller code.

Common parameter patterns to recover:

- direct access: `req.body.name`, `req.query.page`, `req.params.id`
- bracket access: `req.body["name"]`, `req.headers["x-tenant"]`
- destructuring: `const { page, size } = req.query`
- validators: `schema.parse(req.body)`, `validateBody(CreateSchema)`
- NestJS decorators: `@Body() dto: CreateDto`, `@Query("page")`, `@Param("id")`
- FastAPI defaults: `Query(...)`, `Path(...)`, `Header(...)`, `Body(...)`

## Incremental scope

When using Git diff, include changed files that are likely to affect API behavior:

- route files
- controllers and handlers
- request validators
- DTO/schema models
- OpenAPI spec files

If the diff only changes DTO/schema files or deep business logic with no route evidence, either:

- follow explicit user-selected paths/modules
- or fall back to a wider scan and say that incremental confidence was low

Discovery output should include the effective scan scope and fallback reason when incremental mode widens to a full scan.
